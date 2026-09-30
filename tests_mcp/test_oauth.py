"""OAuth/MCP boundary tests. All identities, grants and data are synthetic."""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from fakes import (BASE, CALLBACK, OWNER, FakeActivityReader, FakeCloudFunctions,
                   FakeReader, owner_identity)
from starlette.testclient import TestClient
from app import create_app, verify_owner, COOKIE
from config import Config, SCOPE
from oauth import ConsentError, digest, key
from store import MemoryStore

class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.config = Config(BASE, "synthetic-project", OWNER["email"], {})
        self.reader = FakeReader()
        self.refresher = FakeCloudFunctions()
        self.activity_reader = FakeActivityReader()
        self.app = create_app(self.config, self.store, owner_identity,
                              self.reader, self.refresher, self.activity_reader)
        self.client = TestClient(self.app, base_url=BASE, follow_redirects=False)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.client_id = self.register()
        self.verifier = "v" * 64
        self.challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).decode().rstrip("=")

    def register(self, **overrides):
        body = {"client_name": "Synthetic client", "redirect_uris": [CALLBACK],
                "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"], "scope": SCOPE, **overrides}
        response = self.client.post("/register", json=body)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertIsNone(response.json().get("client_secret"))
        return response.json()["client_id"]

    def begin(self, **overrides):
        params = {"client_id": self.client_id, "response_type": "code", "redirect_uri": CALLBACK,
                  "scope": SCOPE, "state": "synthetic-state", "resource": BASE + "/mcp",
                  "code_challenge": self.challenge, "code_challenge_method": "S256", **overrides}
        return self.client.get("/authorize", params=params)

    def consent(self):
        response = self.begin()
        self.assertEqual(response.status_code, 302, response.text)
        ticket = parse_qs(urlsplit(response.headers["location"]).fragment)["request"][0]
        r = self.client.post("/consent/start", headers={"Origin": BASE}, json={"request": ticket})
        self.assertEqual(r.status_code, 200, r.text)
        cookie = r.headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        return {"request": ticket, "csrf": r.json()["csrf"], "id_token": "synthetic-owner-login", "decision": "approve"}

    def get_code(self):
        consent = self.consent()
        r = self.client.post("/consent/finish", headers={"Origin": BASE}, json=consent)
        self.assertEqual(r.status_code, 200, r.text)
        query = parse_qs(urlsplit(r.json()["redirect"]).query)
        self.assertEqual(query["state"], ["synthetic-state"])
        self.assertEqual(query["iss"], [BASE])
        return query["code"][0]

    def exchange(self, code, **overrides):
        return self.client.post("/token", data={"grant_type": "authorization_code", "code": code,
            "client_id": self.client_id, "redirect_uri": CALLBACK, "code_verifier": self.verifier,
            "resource": BASE + "/mcp", **overrides})

    def tokens(self):
        r = self.exchange(self.get_code())
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def mcp(self, token, method="tools/list", params=None):
        return self.client.post("/mcp", headers={"Authorization": "Bearer " + token,
            "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"},
            json={"jsonrpc": "2.0", "id": 1, "method": method, **({"params": params} if params else {})})

    def test_discovery_and_no_anonymous_tool_access(self):
        r = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(r.status_code, 401)
        self.assertIn("oauth-protected-resource/mcp", r.headers["www-authenticate"])
        d = self.client.get("/.well-known/oauth-protected-resource/mcp").json()
        self.assertEqual(d["resource"], BASE + "/mcp")
        meta = self.client.get("/.well-known/oauth-authorization-server").json()
        self.assertEqual(meta["code_challenge_methods_supported"], ["S256"])
        self.assertEqual(meta["token_endpoint_auth_methods_supported"], ["none"])

    def test_end_to_end_initialize_list_and_call_status_tool(self):
        t = self.tokens()
        init = self.mcp(t["access_token"], "initialize", {"protocolVersion": "2025-11-25",
            "capabilities": {}, "clientInfo": {"name": "synthetic-test", "version": "1"}})
        self.assertEqual(init.status_code, 200, init.text)
        self.assertIn("serverInfo", init.json()["result"])
        r = self.mcp(t["access_token"])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual({x["name"] for x in r.json()["result"]["tools"]},
                         {"get_data_status", "get_portfolio", "list_activities"})
        call = self.mcp(t["access_token"], "tools/call", {"name": "get_data_status", "arguments": {}})
        self.assertEqual(call.status_code, 200, call.text)
        data = call.json()["result"]["structuredContent"]
        self.assertEqual(data["owner_url"], BASE + "/owner")

    def test_wrong_owner_and_forged_identity_denied(self):
        body = self.consent()
        body["id_token"] = "synthetic-stranger-login"
        r = self.client.post("/consent/finish", headers={"Origin": BASE}, json=body)
        self.assertEqual(r.status_code, 403)
        self.assertFalse(any(k.startswith("code_") for k in self.store.data))

    def test_csrf_cookie_and_origin_required(self):
        body = self.consent()
        r = self.client.post("/consent/finish", headers={"Origin": "https://evil.example"}, json=body)
        self.assertEqual(r.status_code, 403)
        bad = {**body, "csrf": "wrong"}
        self.assertEqual(self.client.post("/consent/finish", headers={"Origin": BASE}, json=bad).status_code, 403)
        self.client.cookies.clear()
        self.assertEqual(self.client.post("/consent/finish", headers={"Origin": BASE}, json=body).status_code, 403)

    def test_pkce_and_resource_enforced_before_issue(self):
        code = self.get_code()
        self.assertEqual(self.exchange(code, code_verifier="wrong").status_code, 400)
        self.assertEqual(self.exchange(code, resource="https://evil.example/mcp").json()["error"], "invalid_target")
        self.assertEqual(self.exchange(code).status_code, 200)

    def test_wrong_callback_client_scope_or_pkce_rejected(self):
        for overrides in [{"redirect_uri": "https://evil.example/callback"}, {"scope": "trade.write"},
                          {"code_challenge_method": "plain"}, {"resource": "https://evil.example/mcp"}]:
            r = self.begin(**overrides)
            self.assertTrue(r.status_code == 400 or "error=" in r.headers.get("location", ""))
            if "location" in r.headers:
                self.assertIn("iss=", r.headers["location"])
        r = self.client.post("/register", json={"redirect_uris": ["https://evil.example"],
            "token_endpoint_auth_method": "none", "scope": SCOPE})
        self.assertEqual(r.status_code, 400)

    def test_claude_registers_without_scope(self):
        # Claude's registration omits scope; the server fills in the only one it offers.
        r = self.client.post("/register", json={"client_name": "Claude",
            "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"], "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})
        self.assertEqual(r.status_code, 201, r.text)
        r = self.client.post("/register", json={"redirect_uris": ["https://claude.ai/api/mcp/other"],
            "token_endpoint_auth_method": "none", "scope": SCOPE})
        self.assertEqual(r.status_code, 400)

    def test_gemini_registers_production_callbacks_for_this_host(self):
        # Gemini's real registration: three hosts, each under /r/ and /a/, naming this host.
        tail = "/user_bound_custom-mcp-123456789-connector_example"
        uris = [f"https://oauth-redirect{env}.googleusercontent.com/{kind}{tail}"
                for kind in "ra" for env in ("-sandbox", "-test", "")]
        r = self.client.post("/register", json={"client_name": "Google", "redirect_uris": uris,
            "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"]})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["redirect_uris"], [f"https://oauth-redirect.googleusercontent.com/{k}{tail}" for k in "ra"])
        for bad in ["https://oauth-redirect.googleusercontent.com/r/user_bound_custom-mcp-1-other_example",
                    "https://oauth-redirect.googleusercontent.com/x" + tail,
                    "https://oauth-redirect-sandbox.googleusercontent.com/r" + tail]:
            r = self.client.post("/register", json={"redirect_uris": [bad], "token_endpoint_auth_method": "none"})
            self.assertEqual(r.status_code, 400, bad)

    def test_rejected_registration_is_logged_without_query_strings(self):
        with self.assertLogs(level="WARNING") as logs:
            r = self.client.post("/register", json={"client_name": "Gemini",
                "redirect_uris": ["https://oauth-redirect.googleusercontent.com/r/x?state=secret"],
                "token_endpoint_auth_method": "client_secret_basic", "scope": SCOPE})
        self.assertEqual(r.status_code, 400)
        line = next(m for m in logs.output if "register_rejected" in m)
        self.assertIn("client_secret_basic", line)
        self.assertIn("https://oauth-redirect.googleusercontent.com/r/x", line)
        self.assertNotIn("state=secret", line)

    def test_code_reuse_revokes_grant(self):
        code = self.get_code()
        token = self.exchange(code).json()["access_token"]
        self.assertEqual(self.exchange(code).status_code, 400)
        self.assertEqual(self.mcp(token).status_code, 401)

    def test_parallel_code_exchange_issues_one_grant(self):
        code = self.get_code()
        with ThreadPoolExecutor(2) as pool:
            responses = list(pool.map(lambda _: self.exchange(code), range(2)))
        self.assertEqual(sorted(x.status_code for x in responses), [200, 400])
        token = next(x.json()["access_token"] for x in responses if x.status_code == 200)
        self.assertEqual(self.mcp(token).status_code, 401)

    def test_refresh_rotation_reuse_and_revocation(self):
        t = self.tokens()
        body = {"grant_type": "refresh_token", "refresh_token": t["refresh_token"],
                "client_id": self.client_id, "resource": BASE + "/mcp"}
        r = self.client.post("/token", data=body)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotEqual(r.json()["refresh_token"], t["refresh_token"])
        self.assertEqual(self.client.post("/token", data=body).status_code, 400)
        self.assertEqual(self.mcp(r.json()["access_token"]).status_code, 401)

    def test_refresh_may_omit_resource_but_not_name_another(self):
        # Gemini sends resource when exchanging the code but not when refreshing.
        t = self.tokens()
        body = {"grant_type": "refresh_token", "refresh_token": t["refresh_token"], "client_id": self.client_id}
        other = self.client.post("/token", data={**body, "resource": "https://evil.example/mcp"})
        self.assertEqual(other.json()["error"], "invalid_target")
        r = self.client.post("/token", data=body)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.mcp(r.json()["access_token"]).status_code, 200)
        # A code exchange still has to name this endpoint.
        code = self.get_code()
        r = self.client.post("/token", data={"grant_type": "authorization_code", "code": code,
            "client_id": self.client_id, "redirect_uri": CALLBACK, "code_verifier": self.verifier})
        self.assertEqual(r.json()["error"], "invalid_target")

    def test_revoke_and_expiry(self):
        t = self.tokens()
        self.store.data[key("access", t["access_token"])]["expires_at"] = 0
        self.assertEqual(self.mcp(t["access_token"]).status_code, 401)
        r = self.client.post("/revoke", data={"client_id": self.client_id, "token": t["refresh_token"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(next(v["revoked"] for k,v in self.store.data.items() if k.startswith("grant_")))

    def test_store_does_not_contain_raw_code_or_bearer_tokens(self):
        code = self.get_code()
        t = self.exchange(code).json()
        stored = repr(self.store.data)
        for raw in [code, t["access_token"], t["refresh_token"], "synthetic-owner-login"]:
            self.assertNotIn(raw, stored)

    def test_wrong_audience_and_owner_configuration_change_rejected(self):
        t = self.tokens()
        self.store.data[key("access", t["access_token"])]["resource"] = "https://other.example/mcp"
        self.assertEqual(self.mcp(t["access_token"]).status_code, 401)

    def test_grant_extends_with_use_and_ends_after_idle(self):
        t = self.tokens()
        body = {"grant_type": "refresh_token", "refresh_token": t["refresh_token"],
                "client_id": self.client_id, "resource": BASE + "/mcp"}
        family = next(k for k in self.store.data if k.startswith("grant_"))
        self.store.data[family]["expires_at"] = time.time() + 60  # about to lapse
        r = self.client.post("/token", data=body)
        self.assertEqual(r.status_code, 200, r.text)
        # Using it pushed the end out to 90 days from now.
        self.assertGreater(self.store.data[family]["expires_at"], time.time() + 89 * 86400)
        self.store.data[family]["expires_at"] = time.time() - 1  # idle too long
        body["refresh_token"] = r.json()["refresh_token"]
        self.assertEqual(self.client.post("/token", data=body).status_code, 400)

    def test_consent_expiry_and_replay(self):
        body = self.consent()
        for k in self.store.data:
            if k.startswith("pending_"):
                self.store.data[k]["expires_at"] = 0
        r = self.client.post("/consent/finish", headers={"Origin": BASE}, json=body)
        self.assertEqual(r.status_code, 403)

    def test_google_owner_verifier_checks_provider_verified_email_and_freshness(self):
        good = {**OWNER, "email_verified": True, "auth_time": __import__('time').time(),
                "firebase": {"sign_in_provider": "google.com"}}
        with patch("app.firebase_auth.verify_id_token", return_value=good):
            self.assertEqual(verify_owner("synthetic", self.config), OWNER)
        for bad in [{**good,"email_verified":False}, {**good,"email":"stranger@example.com"},
                    {**good,"firebase":{"sign_in_provider":"custom"}}, {**good,"auth_time":0}]:
            with patch("app.firebase_auth.verify_id_token", return_value=bad):
                with self.assertRaises(ConsentError):
                    verify_owner("synthetic", self.config)

    def test_duplicate_parameters_and_oversize_rejected(self):
        r = self.client.post("/token", content="resource=x&resource=y", headers={"Content-Type":"application/x-www-form-urlencoded"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.client.post("/register", content=b"x"*20000).status_code, 413)


if __name__ == "__main__":
    unittest.main()
