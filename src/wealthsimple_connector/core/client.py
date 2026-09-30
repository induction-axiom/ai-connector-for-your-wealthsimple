"""Read a fixed, read-only Wealthsimple portfolio snapshot.

The reader owns the complete upstream read flow: validate the session, collect
all account and position pages, fetch balances, and return one snapshot. Cloud
locking and session persistence deliberately live outside this module.
"""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from curl_cffi import requests


TOKEN_URL = "https://api.production.wealthsimple.com/v1/oauth/v2/token"
INFO_URL = TOKEN_URL + "/info"
GRAPHQL_URL = "https://my.wealthsimple.com/graphql"
LOGIN_URL = "https://my.wealthsimple.com/app/login"
EXPECTED_SCOPES = frozenset({"invest.read", "trade.read"})
MAX_PAGES = 20
QUERY_DIR = Path(__file__).resolve().parent / "queries"
ACCOUNTS_QUERY = (QUERY_DIR / "accounts.graphql").read_text()
POSITIONS_QUERY = (QUERY_DIR / "positions.graphql").read_text()
BALANCES_QUERY = (QUERY_DIR / "balances.graphql").read_text()
ACTIVITIES_QUERY = (QUERY_DIR / "activities.graphql").read_text()


class PortfolioError(Exception):
    """A fixed, non-sensitive error code."""


class MfaRequired(PortfolioError):
    """Wealthsimple wants a verification code; says where it went when the reply names it."""

    def __init__(self, method=None, hint=None):
        super().__init__("mfa_required")
        self.method = method
        self.hint = hint


# The ways Wealthsimple's web app knows to deliver a code; "app" is an authenticator app.
OTP_METHODS = frozenset({"app", "sms", "recovery_sms", "email"})


def otp_challenge(header):
    """Read a challenge such as `required; method=sms; digits=1234`, keeping only a known
    method and, for text messages, the phone number's last digits."""
    fields = {}
    if isinstance(header, str):
        for part in header.split(";"):
            key, _, value = part.strip().partition("=")
            fields[key] = value
    method = fields.get("method")
    if method not in OTP_METHODS:
        return MfaRequired()
    hint = fields.get("digits") if method in {"sms", "recovery_sms"} else None
    return MfaRequired(method, hint if hint and re.fullmatch(r"\d{2,4}", hint) else None)


class AccessTokenExpired(PortfolioError):
    """The caller may rotate the session once and restart the whole read."""

    def __init__(self):
        super().__init__("access_token_rejected")


@dataclass
class Session:
    client_id: str | None = None
    access_token: str | None = None
    refresh_token: str | None = None
    session_id: str | None = None
    wssdi: str | None = None
    token_info: dict | None = None

    @classmethod
    def from_json(cls, raw: str) -> "Session":
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError
            session = cls(**value)
        except (TypeError, ValueError, json.JSONDecodeError):
            raise PortfolioError("session_secret_invalid") from None

        for name in ("client_id", "access_token", "refresh_token", "session_id", "wssdi"):
            value = getattr(session, name)
            if not isinstance(value, str) or not value:
                raise PortfolioError("session_secret_incomplete")

        session.token_info = None
        return session

    def to_json(self) -> str:
        value = asdict(self)
        value["token_info"] = None
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class WealthsimpleReader:
    """A narrow client that only returns the project's fixed portfolio view."""

    def __init__(self, session: Session):
        self.session = session
        self.deadline = time.monotonic() + 240
        self.http = requests.Session(impersonate="chrome")

    def close(self):
        self.http.close()

    def bootstrap_session(self) -> Session:
        """Create the public, non-credential portion of a first cloud session."""
        response = self._request(
            method="GET", url=LOGIN_URL, headers={}, timeout=20,
            allow_redirects=False, verify=True,
        )
        self._check_status(response, "login_bootstrap")
        device_id = response.cookies.get("wssdi")
        if (not isinstance(device_id, str)
                or not re.fullmatch(r"[A-Fa-f0-9-]{16,128}", device_id)):
            raise PortfolioError("login_bootstrap_device_missing")

        match = re.search(
            r'<script[^>]+src=["\']([^"\']*/app-[a-f0-9]+\.js)["\']',
            response.text,
            re.IGNORECASE,
        )
        if not match:
            raise PortfolioError("login_bootstrap_bundle_missing")
        bundle_url = match.group(1)
        parsed = urlsplit(bundle_url)
        if (parsed.scheme != "https" or parsed.hostname not in {
                "my.wealthsimple.com", "assets.wealthsimple.com"}
                or parsed.username or parsed.password or parsed.port not in (None, 443)
                or parsed.query or parsed.fragment
                or not re.fullmatch(r"/[A-Za-z0-9_./-]*app-[a-f0-9]+\.js", parsed.path,
                                    re.IGNORECASE)):
            raise PortfolioError("login_bootstrap_bundle_untrusted")

        bundle = self._request(
            method="GET", url=bundle_url, headers={}, timeout=20,
            allow_redirects=False, verify=True,
        )
        self._check_status(bundle, "login_bootstrap_bundle")
        match = re.search(
            r'"production"[^}]*clientId:"([a-f0-9]+)"',
            bundle.text,
            re.IGNORECASE,
        )
        if not match:
            raise PortfolioError("login_bootstrap_client_missing")

        self.session.client_id = match.group(1)
        self.session.session_id = str(uuid4())
        self.session.wssdi = device_id
        return self.session

    def collect(self, currency="CAD"):
        """Read one complete snapshot, without refreshing or persisting tokens."""
        if currency not in {"CAD", "USD"}:
            raise PortfolioError("currency_invalid")

        started = utc_now()
        identity_id = self._identity_id()
        accounts, account_pages = self._collect_accounts(identity_id)
        positions, position_pages = self._collect_positions(identity_id, currency)
        balances = self._collect_balances(accounts)

        return {
            "provider": "wealthsimple",
            "started_at": started,
            "fetched_at": utc_now(),
            "source_as_of": None,
            "valuation_as_of": None,
            "requested_currency_view": currency,
            "coverage": "Selected fields; pagination exhausted; completeness and valuations not reconciled.",
            "accounts": accounts,
            "positions": positions,
            "trading_balances": balances,
            "page_observations": {"accounts": account_pages, "positions": position_pages},
        }

    def activity_accounts(self):
        """Return every supported account, including closed accounts, in stable order."""
        accounts, _ = self._collect_accounts(self._identity_id())
        kinds = {"ca_cash_msb", "ca_credit_card", "tfsa", "fhsa", "rrsp",
                 "non_registered", "non_registered_crypto"}
        return sorted((account for account in accounts if account.get("type") in kinds),
                      key=lambda account: account["id"])

    def activity_page(self, account_id, window_start, window_end, cursor=None):
        """Read one activity page for an account and time window."""
        data = self._graphql("ListActivities", ACTIVITIES_QUERY, {
            "first": 50, "cursor": cursor,
            "condition": {"accountIds": [account_id],
                          "startDate": window_start, "endDate": window_end},
        })
        try:
            connection = data["activityFeedItems"]
            nodes = [edge["node"] for edge in connection["edges"]]
            info = connection["pageInfo"]
            has_next = info["hasNextPage"]
            next_cursor = info.get("endCursor") if has_next else None
            if (len(nodes) > 50 or type(has_next) is not bool
                    or (has_next and not nodes)
                    or (has_next and (not isinstance(next_cursor, str)
                                      or not next_cursor or next_cursor == cursor))
                    or any(not isinstance(node, dict)
                           or node.get("accountId") != account_id for node in nodes)):
                raise TypeError
        except (KeyError, TypeError):
            raise PortfolioError("activity_shape_changed") from None
        return nodes, next_cursor

    def refresh_session(self) -> Session:
        """Rotate tokens in memory; the cloud service persists them immediately."""
        response = self._request(
            method="POST",
            url=TOKEN_URL,
            json={
                "grant_type": "refresh_token",
                "refresh_token": self.session.refresh_token,
                "client_id": self.session.client_id,
            },
            headers={
                "Content-Type": "application/json",
                "x-wealthsimple-client": "@wealthsimple/wealthsimple",
                "x-ws-profile": "invest",
                "x-ws-session-id": self.session.session_id,
                "x-ws-device-id": self.session.wssdi,
            },
            timeout=20,
            allow_redirects=False,
            verify=True,
        )
        if response.status_code == 429:
            raise PortfolioError("rate_limited_stop")
        if 300 <= response.status_code < 400:
            raise PortfolioError("redirect_requires_review")

        body = self._json(response)
        if (response.status_code != 200 or body.get("error")
                or not body.get("access_token") or not body.get("refresh_token")):
            raise PortfolioError("refresh_failed_reauth_may_be_required")

        self.session.access_token = body["access_token"]
        self.session.refresh_token = body["refresh_token"]
        self.session.token_info = None
        return self.session

    def reconnect_session(self, username, password, otp=None) -> Session:
        """Replace an expired session using owner-supplied credentials."""
        headers = {
            "Content-Type": "application/json",
            "x-wealthsimple-client": "@wealthsimple/wealthsimple",
            "x-ws-profile": "undefined",
            "x-ws-session-id": self.session.session_id,
            "x-ws-device-id": self.session.wssdi,
        }
        if otp:
            headers["x-wealthsimple-otp"] = otp + ";remember=true"
        response = self._request(
            method="POST",
            url=TOKEN_URL,
            json={
                "grant_type": "password",
                "username": username,
                "password": password,
                "skip_provision": "true",
                "scope": "invest.read trade.read",
                "client_id": self.session.client_id,
                "otp_claim": None,
            },
            headers=headers,
            timeout=20,
            allow_redirects=False,
            verify=True,
        )
        if response.status_code == 429:
            raise PortfolioError("rate_limited_stop")
        if 300 <= response.status_code < 400:
            raise PortfolioError("redirect_requires_review")

        body = self._json(response)
        if response.status_code != 200 or body.get("error"):
            if otp is None and body.get("error") == "invalid_grant":
                raise otp_challenge(response.headers.get("x-wealthsimple-otp"))
            raise PortfolioError("login_rejected")
        if (not isinstance(body.get("access_token"), str) or not body["access_token"]
                or not isinstance(body.get("refresh_token"), str)
                or not body["refresh_token"]):
            raise PortfolioError("login_response_invalid")

        self.session.access_token = body["access_token"]
        self.session.refresh_token = body["refresh_token"]
        self.session.token_info = None
        self._identity_id()
        return self.session

    def _request(self, **kwargs):
        remaining = self.deadline - time.monotonic()
        if remaining <= 1:
            raise PortfolioError("sync_deadline_reached")
        kwargs["timeout"] = min(kwargs.get("timeout", 30), remaining)
        return self.http.request(**kwargs)

    def _headers(self, *, graphql=False, info=False):
        headers = {
            "x-wealthsimple-client": "@wealthsimple/wealthsimple",
            "x-ws-session-id": self.session.session_id,
            "x-ws-device-id": self.session.wssdi,
        }
        if graphql:
            headers.update({
                "Content-Type": "application/json",
                "x-ws-profile": "trade",
                "x-ws-api-version": "12",
                "x-ws-locale": "en-CA",
                "x-platform-os": "web",
            })
        if graphql or info:
            headers["Authorization"] = "Bearer " + self.session.access_token
        return {key: value for key, value in headers.items() if value is not None}

    @staticmethod
    def _json(response):
        try:
            value = json.loads(response.text, parse_float=str)
        except (TypeError, ValueError, json.JSONDecodeError):
            raise PortfolioError("upstream_response_not_json") from None
        if not isinstance(value, dict):
            raise PortfolioError("upstream_response_invalid")
        return value

    @staticmethod
    def _check_status(response, kind):
        if response.status_code == 429:
            raise PortfolioError("rate_limited_stop")
        if 300 <= response.status_code < 400:
            raise PortfolioError("redirect_requires_review")
        if response.status_code == 401:
            raise AccessTokenExpired()
        if response.status_code != 200:
            raise PortfolioError(kind + "_http_failed")

    def _identity_id(self):
        response = self._request(
            method="GET",
            url=INFO_URL,
            headers=self._headers(info=True),
            timeout=20,
            allow_redirects=False,
            verify=True,
        )
        self._check_status(response, "token_info")
        body = self._json(response)

        if (body.get("message") or "").rstrip(".") == "Not Authorized":
            raise AccessTokenExpired()
        if body.get("error") or body.get("errors"):
            raise PortfolioError("token_info_failed")

        scopes = body.get("scope", body.get("scopes"))
        if isinstance(scopes, str):
            scopes = scopes.split()
        if not isinstance(scopes, list) or not all(isinstance(item, str) for item in scopes):
            raise PortfolioError("granted_scopes_unknown")
        if set(scopes) != EXPECTED_SCOPES:
            raise PortfolioError("granted_scopes_differ_from_request")

        identity_id = body.get("identity_canonical_id")
        if not isinstance(identity_id, str) or not identity_id:
            raise PortfolioError("identity_id_missing")
        return identity_id

    def _graphql(self, operation, query, variables):
        response = self._request(
            method="POST",
            url=GRAPHQL_URL,
            json={"operationName": operation, "query": query, "variables": variables},
            headers=self._headers(graphql=True),
            timeout=30,
            allow_redirects=False,
            verify=True,
        )
        self._check_status(response, "graphql")
        body = self._json(response)
        errors = body.get("errors")
        if errors:
            if isinstance(errors, list) and any(
                isinstance(error, dict)
                and isinstance(error.get("extensions"), dict)
                and error["extensions"].get("code") == "UNAUTHENTICATED"
                for error in errors
            ):
                raise AccessTokenExpired()
            raise PortfolioError("graphql_errors_" + operation)

        data = body.get("data")
        if not isinstance(data, dict):
            raise PortfolioError("graphql_data_missing")
        return data

    def _accounts_page(self, identity_id, cursor):
        return self._graphql("PortfolioAccounts", ACCOUNTS_QUERY, {
            "identityId": identity_id,
            "first": 50,
            "cursor": cursor,
        })

    def _positions_page(self, identity_id, currency, cursor):
        return self._graphql("PortfolioPositions", POSITIONS_QUERY, {
            "identityId": identity_id,
            "currency": currency,
            "first": 50,
            "cursor": cursor,
        })

    def _balances_batch(self, account_ids):
        return self._graphql("PortfolioBalances", BALANCES_QUERY, {"ids": account_ids})

    @staticmethod
    def _parse_page(connection):
        try:
            edges = connection["edges"]
            page_info = connection["pageInfo"]
            has_next = page_info["hasNextPage"]
            if not isinstance(edges, list) or type(has_next) is not bool:
                raise TypeError
            nodes = [edge["node"] for edge in edges]
            if any(not isinstance(node, dict) or not isinstance(node.get("id"), str) for node in nodes):
                raise TypeError
        except (KeyError, TypeError):
            raise PortfolioError("connection_shape_changed") from None

        observation = {key: connection[key] for key in ("totalCount", "status") if key in connection}
        return nodes, has_next, page_info.get("endCursor"), observation

    @staticmethod
    def _add_nodes(items, nodes, seen_ids):
        for node in nodes:
            if node["id"] in seen_ids:
                raise PortfolioError("duplicate_node_during_pagination")
            seen_ids.add(node["id"])
            items.append(node)

    @staticmethod
    def _next_cursor(cursor, seen_cursors):
        if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
            raise PortfolioError("pagination_cursor_invalid")
        seen_cursors.add(cursor)
        return cursor

    def _collect_accounts(self, identity_id):
        items, observations, seen_ids, seen_cursors = [], [], set(), set()
        cursor = None
        for _ in range(MAX_PAGES):
            try:
                connection = self._accounts_page(identity_id, cursor)["identity"]["accounts"]
            except (KeyError, TypeError):
                raise PortfolioError("response_shape_changed") from None
            nodes, has_next, end_cursor, observation = self._parse_page(connection)
            self._add_nodes(items, nodes, seen_ids)
            observations.append(observation)
            if not has_next:
                return items, observations
            cursor = self._next_cursor(end_cursor, seen_cursors)
        raise PortfolioError("pagination_limit_reached")

    def _collect_positions(self, identity_id, currency):
        items, observations, seen_ids, seen_cursors = [], [], set(), set()
        cursor = None
        for _ in range(MAX_PAGES):
            try:
                data = self._positions_page(identity_id, currency, cursor)
                connection = data["identity"]["financials"]["current"]["positions"]
            except (KeyError, TypeError):
                raise PortfolioError("response_shape_changed") from None
            nodes, has_next, end_cursor, observation = self._parse_page(connection)
            self._add_nodes(items, nodes, seen_ids)
            observations.append(observation)
            if not has_next:
                return items, observations
            cursor = self._next_cursor(end_cursor, seen_cursors)
        raise PortfolioError("pagination_limit_reached")

    def _collect_balances(self, accounts):
        balances = []
        for offset in range(0, len(accounts), 50):
            account_ids = [account["id"] for account in accounts[offset:offset + 50]]
            batch = self._balances_batch(account_ids).get("accounts")
            if not isinstance(batch, list):
                raise PortfolioError("balances_shape_changed")
            balances.extend(batch)
        return balances
