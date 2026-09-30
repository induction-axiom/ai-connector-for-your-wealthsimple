"""SDK OAuth provider: owner consent, PKCE, opaque tokens and atomic rotation.

No password login, JWT signing, or generic upstream proxy is implemented here.
The MCP SDK handles OAuth wire validation; Firebase Admin verifies Google login.
"""
import hashlib
import re
import secrets
import time
from urllib.parse import urlencode, urlsplit

import anyio
from mcp.server.auth.provider import (
    AccessToken, AuthorizationCode, AuthorizeError, RefreshToken,
    RegistrationError, TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken



def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def key(kind, value):
    return kind + "_" + digest(value)


def random_token():
    return secrets.token_urlsafe(32)


# A connected app stays signed in while it is used; 90 days without use ends the grant.
GRANT_IDLE_SECONDS = 90 * 86400
# Claude web, desktop, mobile and Cowork share this callback; Anthropic asks allowlists to include the claude.com one too.
CLAUDE_CALLBACKS = ("https://claude.ai/api/mcp/auth_callback", "https://claude.com/api/mcp/auth_callback")
# Gemini registers six callbacks: production, sandbox and test hosts, each under /r/ and /a/.
# We keep production only. The path names the Gemini user and our host with dots as underscores.
GEMINI_HOST = "https://oauth-redirect.googleusercontent.com/"
GEMINI_UNUSED = ("https://oauth-redirect-sandbox.googleusercontent.com/",
                 "https://oauth-redirect-test.googleusercontent.com/")


class ConsentError(Exception):
    pass


class Provider:
    def __init__(self, config, store, clock=time.time):
        self.config, self.store, self.clock = config, store, clock

    async def get(self, record):
        return await anyio.to_thread.run_sync(self.store.get, record)

    async def transaction(self, keys, change):
        return await anyio.to_thread.run_sync(self.store.transact, keys, change)

    def valid_redirect(self, uri):
        return (uri in CLAUDE_CALLBACKS or uri == "https://chatgpt.com/connector_platform_oauth_redirect"
                or bool(re.fullmatch(r"https://chatgpt\.com/connector/oauth/[A-Za-z0-9_-]{1,160}", uri))
                or self.gemini_redirect(uri)
                or uri in self.config.test_redirects)

    def gemini_redirect(self, uri):
        ours = re.escape(urlsplit(self.config.base_url).netloc.replace(".", "_"))
        return bool(re.fullmatch(re.escape(GEMINI_HOST) + r"[ra]/user_bound_custom-mcp-[0-9]{1,40}-" + ours, uri))

    async def get_client(self, client_id):
        if len(client_id) > 128:
            return None
        d = await self.get(key("client", client_id))
        return OAuthClientInformationFull.model_validate(d["client"]) if d else None

    async def register_client(self, client_info):
        if client_info.token_endpoint_auth_method != "none" or client_info.client_secret:
            raise RegistrationError("invalid_client_metadata", "Use public-client PKCE (none)")
        uris = [u for u in client_info.redirect_uris or [] if not str(u).startswith(GEMINI_UNUSED)]
        if not 1 <= len(uris) <= 6 or not all(self.valid_redirect(str(u)) for u in uris):
            raise RegistrationError("invalid_redirect_uri", "Only Gemini, Claude and ChatGPT callbacks are allowed")
        client_info.redirect_uris = uris
        if (set(client_info.grant_types) - {"authorization_code", "refresh_token"}
                or set(client_info.response_types) != {"code"}
                or set((client_info.scope or "").split()) != {self.config.scope}):
            raise RegistrationError("invalid_client_metadata", "Unsupported grant or scope")
        record = key("client", client_info.client_id)
        await self.transaction([record], lambda d: (None, {record: {
            "client": client_info.model_dump(mode="json"), "created_at": self.clock()}}))

    async def authorize(self, client, params):
        if params.resource != self.config.resource:
            raise AuthorizeError("invalid_target", "The resource must match this MCP endpoint")
        if params.scopes != [self.config.scope] or not params.state or len(params.state) > 512:
            raise AuthorizeError("invalid_request", "Scope and state are required")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", params.code_challenge):
            raise AuthorizeError("invalid_request", "PKCE S256 required")
        if not self.valid_redirect(str(params.redirect_uri)):
            raise AuthorizeError("invalid_request", "Invalid callback")
        ticket = random_token()
        record = key("pending", ticket)
        await self.transaction([record], lambda d: (None, {record: {
            "params": params.model_dump(mode="json"), "client_id": client.client_id,
            "expires_at": self.clock() + 600, "purge_at": self.clock() + 3600,
            "consumed": False}}))
        # Fragment is not sent in HTTP request URLs/platform request logs.
        return self.config.base_url + "/consent#" + urlencode({"request": ticket})

    async def start_consent(self, ticket, browser_cookie):
        record = key("pending", ticket)
        browser = browser_cookie or random_token()
        csrf = random_token()
        now = self.clock()

        def change(d):
            pending = d[record]
            if (not pending or pending["expires_at"] <= now or pending["consumed"]
                    or pending["params"]["scopes"] != [self.config.scope]):
                raise ConsentError("authorization_expired")
            if pending.get("browser_hash") and pending["browser_hash"] != digest(browser):
                raise ConsentError("authorization_browser_mismatch")
            updated = {**pending, "browser_hash": digest(browser), "csrf_hash": digest(csrf)}
            view = {"client_id": pending["client_id"], "redirect_uri": pending["params"]["redirect_uri"],
                    "scope": self.config.scope, "csrf": csrf}
            return view, {record: updated}

        return await self.transaction([record], change), browser

    async def finish_consent(self, ticket, browser, csrf, owner, approve=True):
        record = key("pending", ticket)
        code = random_token()
        code_key = key("code", code)
        now = self.clock()
        if (not owner or owner.get("email", "").casefold() != self.config.owner_email
                or not owner.get("uid")):
            raise ConsentError("owner_required")

        def change(d):
            p = d[record]
            if (not p or p["expires_at"] <= now or p["consumed"] or not browser or not csrf
                    or p.get("browser_hash") != digest(browser) or p.get("csrf_hash") != digest(csrf)
                    or p["params"]["scopes"] != [self.config.scope]):
                raise ConsentError("authorization_expired_or_invalid")
            params = p["params"]
            values = {"state": params["state"], "iss": self.config.base_url}
            writes = {record: {**p, "consumed": True}}
            if approve:
                values["code"] = code
                writes[code_key] = {**params, "client_id": p["client_id"],
                    "subject": owner["uid"], "owner_email": self.config.owner_email,
                    "expires_at": now + 120, "purge_at": now + 7 * 86400,
                    "used": False}
            else:
                values["error"] = "access_denied"
            return params["redirect_uri"] + "?" + urlencode(values), writes

        return await self.transaction([record], change)

    async def load_authorization_code(self, client, authorization_code):
        d = await self.get(key("code", authorization_code))
        if not d or d["client_id"] != client.client_id:
            return None
        return AuthorizationCode(code=authorization_code, **{k: d[k] for k in (
            "scopes", "expires_at", "client_id", "code_challenge", "redirect_uri",
            "redirect_uri_provided_explicitly", "resource", "subject")})

    async def _exchange(self, source_key, client_id, scopes, refresh):
        original = await self.get(source_key)
        if not original:
            raise TokenError("invalid_grant")
        family_key = original.get("family") or key("grant", source_key)
        access, new_refresh = random_token(), random_token()
        access_key, refresh_key = key("access", access), key("refresh", new_refresh)
        now = int(self.clock())

        def change(d):
            source, family = d[source_key], d[family_key]
            if (not source or source["client_id"] != client_id or source["expires_at"] <= now
                    or source["resource"] != self.config.resource
                    or source["owner_email"] != self.config.owner_email
                    or source["scopes"] != [self.config.scope] or scopes != [self.config.scope]):
                return False, {}
            if source["used"]:
                return False, {family_key: {**family, "revoked": True}} if family else {}
            if refresh and (not family or family["revoked"] or family["expires_at"] <= now):
                return False, {}
            family = {**(family or {"subject": source["subject"], "owner_email": source["owner_email"],
                "client_id": client_id, "resource": self.config.resource, "scopes": scopes,
                "revoked": False, "connected_at": now}),
                "last_used_at": now, "expires_at": now + GRANT_IDLE_SECONDS,
                "purge_at": now + GRANT_IDLE_SECONDS + 86400}
            common = {"client_id": client_id, "resource": self.config.resource,
                      "subject": family["subject"], "owner_email": family["owner_email"],
                      "scopes": scopes, "family": family_key, "purge_at": family["purge_at"]}
            return True, {
                source_key: {**source, "used": True, "family": family_key},
                family_key: family,
                access_key: {**common, "expires_at": now + 900},
                refresh_key: {**common, "expires_at": family["expires_at"], "used": False},
            }

        ok = await self.transaction([source_key, family_key], change)
        if not ok:
            raise TokenError("invalid_grant", "Grant expired, revoked, or already used")
        return OAuthToken(access_token=access, refresh_token=new_refresh,
                          token_type="Bearer", expires_in=900, scope=" ".join(scopes))

    async def exchange_authorization_code(self, client, authorization_code):
        return await self._exchange(key("code", authorization_code.code), client.client_id,
                                    authorization_code.scopes, False)

    async def load_refresh_token(self, client, refresh_token):
        d = await self.get(key("refresh", refresh_token))
        if not d or d["client_id"] != client.client_id:
            return None
        return RefreshToken(token=refresh_token, **{k: d[k] for k in (
            "client_id", "scopes", "expires_at", "resource", "subject")})

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        return await self._exchange(key("refresh", refresh_token.token), client.client_id, scopes, True)

    async def load_access_token(self, token):
        d = await self.get(key("access", token))
        if (not d or d["expires_at"] <= self.clock() or d["resource"] != self.config.resource
                or d["scopes"] != [self.config.scope]):
            return None
        family = await self.get(d["family"])
        if (not family or family["revoked"] or family["expires_at"] <= self.clock()
                or family["owner_email"] != self.config.owner_email or d["owner_email"] != self.config.owner_email):
            return None
        return AccessToken(token=token, **{k: d[k] for k in (
            "client_id", "scopes", "expires_at", "resource", "subject")},
            claims={"iss": self.config.base_url})

    async def revoke_token(self, token):
        kind = "refresh" if isinstance(token, RefreshToken) else "access"
        d = await self.get(key(kind, token.token))
        if not d:
            return
        family = d["family"]
        await self.transaction([family], lambda docs: (None, {
            family: {**docs[family], "revoked": True}} if docs[family] else {}))

    def connected_apps(self):
        """Clients holding a live grant, for the dashboard."""
        now = self.clock()
        clients = {d["client"]["client_id"]: d for d in self.store.list("client_").values()}
        apps = {}
        for family in self.store.list("grant_").values():
            client = clients.get(family.get("client_id"))
            if not client or family.get("revoked") or family.get("expires_at", 0) <= now:
                continue
            info = client["client"]
            app = apps.setdefault(info["client_id"], {
                "client_id": info["client_id"],
                "name": info.get("client_name") or None,
                "redirect_host": urlsplit(str(info["redirect_uris"][0])).hostname,
                "registered_at": client.get("created_at"),
                "connected_at": None,
                "last_used_at": None,
            })
            # Grants made before connected_at existed fall back to the app's registration.
            connected = family.get("connected_at") or app["registered_at"]
            if connected and (app["connected_at"] is None or connected < app["connected_at"]):
                app["connected_at"] = connected
            used = family.get("last_used_at")
            if used and (app["last_used_at"] is None or used > app["last_used_at"]):
                app["last_used_at"] = used
        return sorted(apps.values(), key=lambda app: app["last_used_at"] or 0, reverse=True)

    def disconnect_app(self, client_id):
        """Revoke every grant of one client; it must ask the owner again."""
        families = [k for k, v in self.store.list("grant_").items()
                    if v.get("client_id") == client_id and not v.get("revoked")]
        if families:
            self.store.transact(families, lambda docs: (None, {
                k: {**v, "revoked": True} for k, v in docs.items() if v}))
        return len(families)

    async def rate_limit(self, group, maximum):
        now = self.clock()
        record = "limit_" + group + "_" + str(int(now // 60))

        def change(d):
            count = (d[record] or {}).get("count", 0)
            return count < maximum, {record: {"count": count + 1, "purge_at": now + 3600}}

        return await self.transaction([record], change)
