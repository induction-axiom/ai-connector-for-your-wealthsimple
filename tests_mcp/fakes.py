"""Synthetic identities, snapshots and cloud stand-ins shared by the MCP tests."""
from datetime import datetime, timezone
import operator
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "firebase" / "mcp"))
sys.path.insert(0, str(ROOT / "src"))
from wealthsimple_connector.core.portfolio import project_portfolio

BASE = "https://connector.example"
CALLBACK = "https://chatgpt.com/connector_platform_oauth_redirect"
OWNER = {"uid": "synthetic-owner-id", "email": "owner@example.com"}


def owner_identity(token, max_age=None):
    if token == "synthetic-owner-login":
        return OWNER
    return {"uid": "stranger", "email": "stranger@example.com"}


def fixture():
    return {"provider": "wealthsimple", "fetched_at": datetime.now(timezone.utc).isoformat(),
        "requested_currency_view": "CAD", "password": "NEVER-EXPORT",
        "accounts": [{"id": "private-account-id", "type": "tfsa", "status": "open", "currency": "CAD",
            "financials": {"currentCombined": {"netLiquidationValue": {"amount": "123.4500", "currency": "CAD"}}},
            "accountNumber": "NEVER-EXPORT"}],
        "positions": [{"id": "private-position-id", "accounts": [{"id": "private-account-id"}],
            "quantity": "2.0001", "security": {"stock": {"symbol": "TEST"}},
            "totalValue": {"amount": "12.34", "currency": "USD"}, "bookValue": None}],
        "trading_balances": [{"custodianAccounts": [{"id": "NEVER-EXPORT"}]}]}


class FakeReader:
    def __init__(self): self.calls = 0
    def portfolio(self, **kwargs):
        self.calls += 1
        return project_portfolio(fixture(), {"status": "ok"}, **kwargs)
    def status(self):
        return {"snapshot_available": True,
                "connection_state": "connected", "account_count": 1,
                "position_count": 1, "last_sync_error": None}


class FakeCloudFunctions:
    def __init__(self): self.calls, self.forced = [], []
    def refresh(self, target, force=False):
        self.calls.append(target)
        if force:
            self.forced.append(target)
        return {"result": "refresh_succeeded", "fetched_at": "2026-09-26T22:15:10+00:00"}
    def sign_out(self):
        self.calls.append("sign_out")
        return {"result": "signed_out"}
    def reconnect(self, username, password, otp=None):
        self.calls.append(("reconnect", username, password, otp))
        return {"result": "reconnect_succeeded"}


class FakeActivityDB:
    """In-memory Firestore for ActivityReader: filters, orders, offsets and counts."""
    OPERATORS = {"==": operator.eq, ">=": operator.ge, "<=": operator.le}

    def __init__(self, state, rows):
        self.state, self.rows = state, rows

    def collection(self, name):
        if name == "connectors":
            return SimpleNamespace(document=lambda _key: SimpleNamespace(
                get=lambda **_: SimpleNamespace(to_dict=lambda: {"activities": self.state})))
        return FakeActivityQuery(self.rows)


class FakeActivityQuery:
    def __init__(self, rows, filters=(), orders=(), skip=0, take=None):
        self.rows, self.filters, self.orders = rows, filters, orders
        self.skip, self.take = skip, take

    def _with(self, **changes):
        return FakeActivityQuery(**{"rows": self.rows, "filters": self.filters,
                                    "orders": self.orders, "skip": self.skip,
                                    "take": self.take, **changes})

    def where(self, filter):
        return self._with(filters=self.filters + (filter,))

    def order_by(self, field, direction):
        return self._with(orders=self.orders + ((field, direction),))

    def offset(self, count):
        return self._with(skip=count)

    def limit(self, count):
        return self._with(take=count)

    def _matches(self):
        # Like Firestore, a row without an ordered or filtered field is not in the index.
        fields = {f.field_path for f in self.filters} | {field for field, _ in self.orders}
        rows = [row for row in self.rows if fields <= row.keys() and all(
            FakeActivityDB.OPERATORS[f.op_string](row[f.field_path], f.value)
            for f in self.filters)]
        for field, direction in reversed(self.orders):
            rows.sort(key=lambda row: row[field], reverse=direction == "DESCENDING")
        return rows

    def stream(self, **_):
        rows = self._matches()[self.skip:]
        rows = rows[:self.take] if self.take is not None else rows
        return [SimpleNamespace(to_dict=lambda row=row: dict(row)) for row in rows]

    def count(self):
        return SimpleNamespace(get=lambda **_: [[SimpleNamespace(value=len(self._matches()))]])


class FakeActivityReader:
    def __init__(self): self.calls = []
    def status(self):
        return {"available": True, "coverage_complete": True,
                "account_count": 1, "rows_processed": 4}
    def recent(self, count=3):
        return [{"activity_ref": "a", "occurred_at": "2026-09-26T20:00:00Z", "type": "DIY_BUY",
                 "asset_symbol": "TEST", "amount": "10.00", "currency": "CAD"}]
    def list(self, **kwargs):
        self.calls.append(kwargs)
        return {"kind": kwargs["kind"], "read_mode": "saved_activities",
                "activities": [], "next_offset": None}
