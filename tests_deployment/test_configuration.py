import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "firebase/scripts"))
from configuration import make_config, function_env, mcp_env
import manage
from manage import bootstrap_config, build, provision, _write_bootstrap_firebase_config

WEB = {"projectId": "your-project", "apiKey": "public-key",
       "authDomain": "your-project.firebaseapp.com", "appId": "app-id"}


def service(owner="owner@example.com", region="us-central1"):
    """The shape `gcloud run services list --format=json` returns, trimmed to what is read."""
    env = [{"name": "MCP_OWNER_EMAIL", "value": owner},
           {"name": "FIREBASE_WEB_CONFIG", "value": json.dumps(WEB)}]
    return {"metadata": {"labels": {"cloud.googleapis.com/location": region}},
            "spec": {"template": {"spec": {"containers": [{"env": env}]}}}}


def fake_gcloud(services, apis="run.googleapis.com"):
    def run(args, **_):
        if args[:3] == ["gcloud", "services", "list"]:
            return apis
        if args[:4] == ["gcloud", "run", "services", "list"]:
            return json.dumps(services)
        if args[:3] == ["gcloud", "projects", "describe"]:
            return "123456"
        raise AssertionError(args)
    return run


class DeploymentTests(unittest.TestCase):
    def config(self, region="us-east4", **updates):
        return make_config("your-project", updates.get("owner_email", "owner@example.com"),
                           region, "123456", WEB)

    def test_parameters_flow_to_both_runtimes(self):
        cfg = self.config(region="us-central1")
        functions = function_env(cfg)
        mcp = mcp_env(cfg, "https://refresh.example.run.app",
                      "https://reconnect.example.run.app")
        self.assertEqual(functions["PORTFOLIO_DATABASE"], mcp["PORTFOLIO_DATABASE"])
        self.assertNotIn("READ_SERVICE_ACCOUNT", functions)
        self.assertIn("us-central1.run.app", mcp["MCP_BASE_URL"])
        self.assertEqual(mcp["REFRESH_FUNCTION_URL"], "https://refresh.example.run.app")
        self.assertEqual(mcp["RECONNECT_FUNCTION_URL"], "https://reconnect.example.run.app")

    def test_reject_invalid_inputs(self):
        for args in [("--unexpected-option", "owner@example.com", "us-central1"),
                     ("your-project", "not-an-email", "us-central1"),
                     ("your-project", "owner@example.com", "us-central1\nSECRET=bad")]:
            with self.subTest(args=args):
                with self.assertRaises(ValueError): make_config(*args)
        with self.assertRaises(ValueError):
            make_config("your-project", "owner@example.com", "us-central1", "123",
                        {**WEB, "projectId": "someone-else"})

    def test_bundle_holds_only_allowlisted_sources_and_starts_clean(self):
        cfg = self.config()
        dest = build(cfg)
        stray = dest / "mcp/accidental-secret.json"
        stray.write_text('{"synthetic_secret":"never-upload"}')
        build(cfg)
        self.assertFalse(stray.exists())
        files = {str(f.relative_to(dest)) for f in dest.rglob("*")
                 if f.is_file() and "venv" not in f.parts}
        self.assertIn("functions/wealthsimple_connector/core/client.py", files)
        self.assertIn("mcp/wealthsimple_connector/core/portfolio.py", files)
        self.assertIn("mcp/static/owner.js", files)
        version = json.loads((dest / "mcp/version.json").read_text())
        self.assertRegex(version["version"], r"^\d+\.\d+\.\d+$")
        self.assertTrue(all("private" not in f and "__pycache__" not in f for f in files))
        self.assertEqual({f for f in files if ".env" in f}, {f"functions/.env.{cfg['project_id']}"})
        self.assertNotIn("firestore", json.loads((dest / "firebase.json").read_text()))
        # The MCP container runs as a non-root user; it must be able to read everything uploaded.
        for path in (dest / "mcp").rglob("*"):
            if path.name not in {"mcp-env.json", ".gcloudignore"}:
                needed = 0o005 if path.is_dir() else 0o004
                self.assertEqual(path.stat().st_mode & needed, needed, path)

    def test_bootstrap_uses_separate_identities_and_databases(self):
        with patch("manage.run", fake_gcloud([])):
            cfg, updating = bootstrap_config("fresh-project-123", "owner@example.com", None)
        self.assertFalse(updating)
        self.assertEqual(cfg["region"], "us-east4")
        self.assertEqual(cfg["auth_database"], "mcp-auth")
        self.assertEqual(cfg["mcp_service"], "wealthsimple-mcp")
        self.assertNotEqual(cfg["sync_service_account"], cfg["mcp_service_account"])
        self.assertNotEqual(cfg["auth_database"], cfg["portfolio_database"])

    def test_a_project_without_cloud_run_turned_on_is_a_first_setup(self):
        with patch("manage.run", fake_gcloud(None, apis="firestore.googleapis.com")):
            _cfg, updating = bootstrap_config("fresh-project-123", "owner@example.com", None)
        self.assertFalse(updating)

    def test_update_finds_the_deployed_connector_without_local_files(self):
        with patch("manage.run", fake_gcloud([service(owner="Owner@Example.com")])):
            cfg, updating = bootstrap_config("your-project", "owner@example.com", None)
            deployed = manage.deployed_config("your-project")
        self.assertTrue(updating)
        self.assertEqual(cfg["region"], "us-central1")
        self.assertEqual(deployed["firebase_web_config"], WEB)
        self.assertEqual(mcp_env(deployed)["MCP_BASE_URL"],
                         "https://wealthsimple-mcp-123456.us-central1.run.app")

    def test_update_refuses_another_owner_or_region(self):
        with patch("manage.run", fake_gcloud([service(owner="someone@example.com")])):
            with self.assertRaisesRegex(RuntimeError, "belongs to someone@example.com"):
                bootstrap_config("your-project", "owner@example.com", None)
        with patch("manage.run", fake_gcloud([service()])):
            with self.assertRaisesRegex(RuntimeError, "runs in us-central1"):
                bootstrap_config("your-project", "owner@example.com", "europe-west1")

    def test_deploy_without_a_connector_points_to_bootstrap(self):
        with patch("manage.run", fake_gcloud([])):
            with self.assertRaisesRegex(RuntimeError, "bootstrap.sh"):
                manage.deployed_config("your-project")

    def test_update_never_recreates_a_missing_identity(self):
        """A missing service account on update stops before anything is created."""
        calls = []

        def cloud(cfg, *args):
            calls.append(args)
            if args[:2] == ("projects", "describe"):
                return json.dumps({"projectNumber": "123456"})
            return "True" if args[0] == "billing" else ""
        with patch("manage.cloud", cloud):
            with self.assertRaisesRegex(RuntimeError, "Service account .* is missing"):
                provision(self.config(), sys.executable, updating=True)
        self.assertFalse([c for c in calls if "create" in c])

    def test_bootstrap_firebase_config_enables_only_google_and_closed_rules(self):
        cfg = self.config()
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            path = _write_bootstrap_firebase_config(cfg, destination)
            value = json.loads(path.read_text())
            providers = value["auth"]["providers"]
            self.assertEqual(set(providers), {"googleSignIn"})
            self.assertEqual(providers["googleSignIn"]["supportEmail"], "owner@example.com")
            # Firebase adds the default handler itself; repeating it is rejected as a duplicate.
            self.assertNotIn("authorizedRedirectUris", providers["googleSignIn"])
            self.assertEqual({item["database"] for item in value["firestore"]},
                             {"(default)", "mcp-auth"})
            self.assertIn("allow read, write: if false", (destination / "firestore.rules").read_text())
