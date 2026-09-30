"""Explicit deployment configuration. No portfolio or Wealthsimple credentials."""
from dataclasses import dataclass
import json
import os
from urllib.parse import urlsplit

SCOPE = "portfolio.read"


@dataclass(frozen=True)
class Config:
    base_url: str
    project_id: str
    owner_email: str
    firebase_config: dict
    database: str = "mcp-auth"
    test_redirects: tuple[str, ...] = ()
    portfolio_database: str = "(default)"
    stale_seconds: int = 25200
    refresh_url: str = ""
    reconnect_url: str = ""
    scope: str = SCOPE

    @property
    def resource(self):
        return self.base_url + "/mcp"

    @classmethod
    def from_env(cls):
        base = os.environ["MCP_BASE_URL"].rstrip("/")
        u = urlsplit(base)
        if u.scheme != "https" or not u.hostname or u.path or u.query or u.fragment or u.username:
            raise ValueError("MCP_BASE_URL must be an HTTPS origin")
        project = os.environ["GOOGLE_CLOUD_PROJECT"]
        owner = os.environ["MCP_OWNER_EMAIL"].casefold()
        if "@" not in owner:
            raise ValueError("MCP_OWNER_EMAIL required")
        public = json.loads(os.environ["FIREBASE_WEB_CONFIG"])
        if public.get("projectId") != project or not public.get("apiKey"):
            raise ValueError("Firebase configuration mismatch")
        database = os.environ["MCP_AUTH_DATABASE"]
        portfolio_db = os.environ.get("PORTFOLIO_DATABASE", "(default)")
        if database in {"(default)", portfolio_db}:
            raise ValueError("OAuth records need their own database")
        urls = {}
        for name in ("REFRESH_FUNCTION_URL", "RECONNECT_FUNCTION_URL"):
            target = urlsplit(os.environ.get(name, ""))
            if (target.scheme != "https" or not (target.hostname or "").endswith(".run.app")
                    or target.path or target.query or target.fragment or target.username):
                raise ValueError(name + " must be a Cloud Run HTTPS origin")
            urls[name] = os.environ[name]
        return cls(base, project, owner, public, database,
                   portfolio_database=portfolio_db,
                   stale_seconds=int(os.environ.get("STALE_SECONDS", "25200")),
                   refresh_url=urls["REFRESH_FUNCTION_URL"],
                   reconnect_url=urls["RECONNECT_FUNCTION_URL"])
