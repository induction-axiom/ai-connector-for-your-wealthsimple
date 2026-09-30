"""Deployment settings: fixed resource names plus the few per-deployment inputs.

Nothing is saved locally. Names never vary, so every run finds the same resources,
and anything a run can't derive it reads back from the deployed connector."""
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REGION = "us-east4"
# One connector per project, so these names identify its resources; changing one strands them.
NAMES = {
    "portfolio_database": "(default)",
    "auth_database": "mcp-auth",
    "session_secret_id": "wealthsimple-session",
    "sync_service_account": "portfolio-sync",
    "mcp_service_account": "portfolio-mcp",
    "mcp_service": "wealthsimple-mcp",
}
VIEW_CURRENCY = "CAD"
STALE_SECONDS = 25200


def make_config(project_id, owner_email, region, project_number=None, firebase_web_config=None):
    patterns = {"project_id": (project_id, r"[a-z][a-z0-9-]{4,28}[a-z0-9]"),
                "region": (region, r"[a-z]+[a-z0-9-]+[0-9]"),
                "owner_email": (owner_email,
                                r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")}
    for name, (value, pattern) in patterns.items():
        if not isinstance(value, str) or not re.fullmatch(pattern, value):
            raise ValueError("Invalid " + name)
    if project_number is not None and not re.fullmatch(r"[0-9]+", str(project_number)):
        raise ValueError("Invalid project number")
    web = firebase_web_config
    if web is not None and (set(web) != {"projectId", "apiKey", "authDomain", "appId"}
                            or web["projectId"] != project_id
                            or web["authDomain"] != project_id + ".firebaseapp.com"
                            or not all(isinstance(x, str) and x and "\n" not in x
                                       for x in web.values())):
        raise ValueError("Firebase Web config must match this project")
    return {**NAMES, "project_id": project_id, "owner_email": owner_email, "region": region,
            "view_currency": VIEW_CURRENCY, "stale_seconds": STALE_SECONDS,
            "project_number": None if project_number is None else str(project_number),
            "firebase_web_config": web}


def email(cfg, role):
    return cfg[role + "_service_account"] + "@" + cfg["project_id"] + ".iam.gserviceaccount.com"


def function_env(cfg):
    return {"CONNECTOR_REGION": cfg["region"], "SESSION_SECRET_ID": cfg["session_secret_id"],
            "PORTFOLIO_DATABASE": cfg["portfolio_database"],
            "VIEW_CURRENCY": cfg["view_currency"], "STALE_SECONDS": str(cfg["stale_seconds"]),
            "SYNC_SERVICE_ACCOUNT": email(cfg, "sync")}


def mcp_env(cfg, refresh_url="", reconnect_url=""):
    base = f'https://{cfg["mcp_service"]}-{cfg["project_number"]}.{cfg["region"]}.run.app'
    return {"MCP_BASE_URL": base, "GOOGLE_CLOUD_PROJECT": cfg["project_id"],
            "MCP_OWNER_EMAIL": cfg["owner_email"].casefold(), "MCP_AUTH_DATABASE": cfg["auth_database"],
            "PORTFOLIO_DATABASE": cfg["portfolio_database"],
            "STALE_SECONDS": str(cfg["stale_seconds"]),
            "FIREBASE_WEB_CONFIG": json.dumps(cfg["firebase_web_config"], separators=(",", ":")),
            "REFRESH_FUNCTION_URL": refresh_url,
            "RECONNECT_FUNCTION_URL": reconnect_url}
