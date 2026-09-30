"""Synthetic fixtures for real-mode output and authorization; no real accounts."""
from dataclasses import replace
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, Mock, patch
from io import BytesIO
from urllib.error import HTTPError

from fakes import BASE, FakeActivityDB, fixture
import test_oauth
from activity_store import ActivityError, ActivityReader
from cloud_functions import CloudFunctions
from config import Config
from portfolio_store import PortfolioReader
from starlette.testclient import TestClient
from wealthsimple_connector.core.activity import (
    ACTIVITY_SCHEMA, activity_category, activity_direction, activity_kind, activity_outcome)
from wealthsimple_connector.core.portfolio import project_portfolio, SnapshotError


class ConfigTests(unittest.TestCase):
    def test_real_mode_requires_both_private_function_urls(self):
        environment = {
            "MCP_BASE_URL": BASE,
            "GOOGLE_CLOUD_PROJECT": "synthetic-project",
            "MCP_OWNER_EMAIL": "owner@example.com",
            "FIREBASE_WEB_CONFIG": json.dumps({
                "projectId": "synthetic-project", "apiKey": "public-key"}),
            "MCP_AUTH_DATABASE": "mcp-auth",
            "REFRESH_FUNCTION_URL": "https://refresh.example.run.app",
            "RECONNECT_FUNCTION_URL": "https://reconnect.example.run.app",
        }
        with patch.dict(os.environ, environment, clear=True):
            config = Config.from_env()
        self.assertEqual(config.reconnect_url, "https://reconnect.example.run.app")
        environment.pop("RECONNECT_FUNCTION_URL")
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(ValueError, "RECONNECT_FUNCTION_URL"):
                Config.from_env()


class ProjectionTests(unittest.TestCase):
    def test_status_can_request_reconnect_before_first_snapshot(self):
        reader = PortfolioReader.__new__(PortfolioReader)
        reader._documents = Mock(return_value=(
            None, {}, {"session": {"reconnect_required": True}}))
        result = reader.status()
        self.assertFalse(result["snapshot_available"])
        self.assertTrue(result["reconnect_required"])
        self.assertEqual(result["connection_state"], "reconnect_required")
        self.assertIsNone(result["account_count"])
        self.assertIsNone(result["position_count"])

    def test_unfinished_rotation_needs_reconnect_once_no_sync_holds_it(self):
        reader = PortfolioReader.__new__(PortfolioReader)
        reader.stale_seconds = 25200
        now = datetime(2026, 9, 27, 3, 0, tzinfo=timezone.utc)
        state = {"session": {"secret_version": "projects/secret/versions/7",
                             "rotation_pending": True},
                 "sync": {"lease_id": "worker", "lease_until": now + timedelta(minutes=5)}}
        reader._documents = Mock(return_value=(
            {"payload_json": json.dumps(fixture())}, {"status": "ok"}, state))
        self.assertEqual(reader.status(now)["connection_state"], "connected")  # rotating now
        state["sync"]["lease_until"] = now - timedelta(seconds=1)
        result = reader.status(now)
        self.assertEqual(result["connection_state"], "reconnect_required")
        self.assertTrue(result["reconnect_required"])

    def test_status_reports_fresh_deployment_as_not_connected(self):
        reader = PortfolioReader.__new__(PortfolioReader)
        reader._documents = Mock(return_value=(None, {}, {}))
        result = reader.status()
        self.assertFalse(result["reconnect_required"])
        self.assertEqual(result["connection_state"], "not_connected")

    def test_status_reports_session_without_exposing_secret_version(self):
        reader = PortfolioReader.__new__(PortfolioReader)
        reader.stale_seconds = 25200
        reader._documents = Mock(return_value=({"payload_json": json.dumps(fixture())}, {
            "status": "ok", "account_count": 1, "position_count": 1,
        }, {"session": {"secret_version": "projects/secret/versions/7"}}))
        result = reader.status()
        self.assertEqual(result["connection_state"], "connected")
        self.assertEqual(result["account_count"], 1)
        self.assertEqual(result["position_count"], 1)
        self.assertNotIn("secret_version", result)
        self.assertNotIn("projects/secret", json.dumps(result))

    def test_allowlist_precision_and_account_join(self):
        result = project_portfolio(fixture(), {"status": "ok"})
        self.assertNotIn("NEVER-EXPORT", json.dumps(result))
        self.assertNotIn("private-account-id", json.dumps(result))
        self.assertEqual(result["positions"][0]["account_refs"], [result["accounts"][0]["account_ref"]])
        self.assertEqual(result["accounts"][0]["reported_net_liquidation_value"]["amount"], "123.4500")
        self.assertEqual(result["accounts"][0]["nickname"],
                         "TFSA · " + result["accounts"][0]["account_ref"][:6])
        self.assertIsNone(result["positions"][0]["reported_book_value"])
        self.assertFalse(result["pricing_realtime_verified"])

    def test_neutral_nicknames_distinguish_two_chequing_and_credit_card(self):
        data = fixture()
        data["accounts"] = [
            {"id": "first-private-id", "type": "ca_cash_msb", "status": "open",
             "currency": "CAD", "accountNumber": "DO-NOT-RETURN"},
            {"id": "second-private-id", "type": "ca_cash_msb", "status": "open",
             "currency": "CAD", "nickname": "PRIVATE UPSTREAM NAME"},
            {"id": "card-private-id", "type": "ca_credit_card", "status": "open",
             "currency": "CAD", "accountNumber": "DO-NOT-RETURN"},
        ]
        result = project_portfolio(data, {"status": "ok"})
        names = [account["nickname"] for account in result["accounts"]]
        self.assertEqual(len(set(names)), 3)
        self.assertTrue(all(name.startswith("Chequing · ") for name in names[:2]))
        self.assertTrue(names[2].startswith("Credit card · "))
        self.assertNotIn("DO-NOT-RETURN", json.dumps(result))
        self.assertNotIn("PRIVATE UPSTREAM NAME", json.dumps(result))
        page = project_portfolio(data, {"status": "ok"}, account_offset=1, limit=1)
        self.assertEqual(page["accounts"][0]["nickname"], names[1])

    def test_failed_sync_old_and_future_snapshots_stale(self):
        data = fixture()
        self.assertTrue(project_portfolio(data, {"status": "error"})["stale"])
        data["fetched_at"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        self.assertTrue(project_portfolio(data, {"status": "ok"})["stale"])
        data["fetched_at"] = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        self.assertTrue(project_portfolio(data, {"status": "ok"})["stale"])

    def test_pagination_and_invalid_snapshot(self):
        data = fixture()
        data["positions"] *= 3
        page = project_portfolio(data, {"status": "ok"}, limit=2)
        self.assertEqual(len(page["positions"]), 2)
        self.assertEqual(page["next_position_offset"], 2)
        self.assertIsNone(page["next_account_offset"])
        with self.assertRaises(SnapshotError): project_portfolio(data, {}, limit=101)
        data["fetched_at"] = "2026-01-01"
        with self.assertRaises(SnapshotError): project_portfolio(data, {})


class RefreshInvocationTests(unittest.TestCase):
    def call(self, method, *args, response=None, error=None):
        opener = Mock()
        if error:
            opener.open.side_effect = error
        else:
            body = MagicMock()
            body.__enter__.return_value = BytesIO(json.dumps(response).encode())
            opener.open.return_value = body
        with patch("cloud_functions.fetch_id_token", return_value="test-token"), \
                patch("cloud_functions.build_opener", return_value=opener):
            functions = CloudFunctions("https://refresh.example.run.app",
                                       "https://reconnect.example.run.app")
            return getattr(functions, method)(*args), opener

    def test_refresh_calls_the_private_function_and_passes_its_result_through(self):
        summary = {"result": "refresh_succeeded", "fetched_at": "2026-09-26T22:15:10+00:00",
                   "account_count": 2, "position_count": 1}
        result, opener = self.call("refresh", "portfolio", response=summary)
        self.assertEqual(result, summary)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://refresh.example.run.app")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-token")
        self.assertEqual(json.loads(request.data), {"target": "portfolio"})

    def test_transport_failures_have_fixed_results(self):
        result, _ = self.call("refresh", "portfolio", error=TimeoutError())
        self.assertEqual(result, {"result": "refresh_result_unknown"})
        result, _ = self.call("reconnect", "owner@example.com", "synthetic-password",
                              error=HTTPError("https://reconnect.example.run.app", 500, "", {}, None))
        self.assertEqual(result, {"result": "reconnect_failed"})

    def test_reconnect_posts_credentials_only_to_the_reconnect_function(self):
        result, opener = self.call("reconnect", "owner@example.com", "synthetic-password",
                                   "123456", response={"result": "mfa_required"})
        self.assertEqual(result, {"result": "mfa_required"})
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://reconnect.example.run.app")
        self.assertEqual(json.loads(request.data)["otp"], "123456")


class ActivityReaderTests(unittest.TestCase):
    @staticmethod
    def saved(row):
        """A row as the sync saves it, with its query fields."""
        return {**row, "occurred_ts": datetime.fromisoformat(
                    row["occurred_at"].replace("Z", "+00:00")).astimezone(timezone.utc),
                "kind": activity_kind(row["account_type"], row["type"], row.get("sub_type"))}

    def test_one_collection_filters_and_pages_all_activity(self):
        fetched = "2026-09-27T02:29:38+00:00"
        status = {"schema": ACTIVITY_SCHEMA, "status": "complete", "last_fetched_at": fetched,
                  "window_start": "1900-01-01T00:00:00Z",
                  "window_end": fetched, "account_count": 3,
                  "earliest_occurred_at": "2020-01-01T00:00:00+00:00"}
        rows = [self.saved(row) for row in [
            {"activity_ref": "a", "account_ref": "a" * 20,
             "account_type": "ca_cash_msb", "account_nickname": "Chequing · aaaaaa",
             "observed_at": fetched, "occurred_at": "2026-09-26T20:00:00Z",
             "type": "SPEND", "sub_type": "PREPAID", "status": "settled",
             "amount": "12.30", "amount_sign": "negative", "currency": "CAD",
             "merchant": "Synthetic Cafe"},
            {"activity_ref": "b", "account_ref": "b" * 20,
             "account_type": "ca_credit_card", "account_nickname": "Credit card · bbbbbb",
             # Later than "a" in time, although earlier as a string.
             "observed_at": fetched, "occurred_at": "2026-09-26T17:00:00-04:00",
             "type": "CREDIT_CARD", "sub_type": "PURCHASE", "status": "authorized",
             "amount": "20.00", "amount_sign": "negative", "currency": "CAD",
             "merchant": "Synthetic Store"},
            {"activity_ref": "c", "account_ref": "c" * 20,
             "account_type": "ca_cash_msb", "observed_at": fetched,
             "occurred_at": "2026-09-26T19:00:00Z", "type": "WITHDRAWAL",
             "sub_type": "E_TRANSFER"},
            {"activity_ref": "d", "account_ref": "d" * 20,
             "account_type": "tfsa", "account_nickname": "TFSA · dddddd",
             "observed_at": fetched, "occurred_at": "2026-09-26T18:00:00Z",
             "type": "DIY_BUY", "status": "FILLED", "amount": None,
             "asset_symbol": "TEST", "asset_quantity": "2"},
        ]]
        reader = ActivityReader("synthetic", "(default)", 25200, db=FakeActivityDB(status, rows))
        now = datetime(2026, 9, 27, 3, 0, tzinfo=timezone.utc)

        spending = reader.list(kind="spending", limit=1, now=now)
        self.assertEqual(spending["matching_count"], 2)
        self.assertEqual(spending["activities"][0]["merchant"], "Synthetic Store")
        self.assertEqual(spending["next_offset"], 1)
        self.assertNotIn("total", spending)
        self.assertNotIn("occurred_ts", spending["activities"][0])
        self.assertEqual({key: spending["activities"][0][key]
                          for key in ("category", "direction", "outcome")},
                         {"category": "purchase", "direction": "out", "outcome": "pending"})
        self.assertNotIn("Synthetic Cafe", json.dumps(spending))
        second = reader.list(kind="spending", limit=1, offset=1, now=now)
        self.assertEqual(second["activities"][0]["merchant"], "Synthetic Cafe")
        self.assertIsNone(second["next_offset"])
        investment = reader.list(kind="investment", now=now)
        self.assertEqual(investment["matching_count"], 1)
        self.assertIsNone(investment["activities"][0]["amount"])
        filtered = reader.list(kind="spending", account_ref="a" * 20, now=now)
        self.assertEqual(filtered["matching_count"], 1)
        all_rows = reader.list(start="2026-09-26T19:30:00Z", now=now)
        self.assertEqual([row["activity_ref"] for row in all_rows["activities"]], ["b", "a"])
        self.assertEqual(spending["accounts_with_history_gaps"], 0)
        status.update(status="partial", account_gaps={"a" * 20: "1900-01-01T00:00:00Z"})
        gap = reader.list(now=now)
        self.assertEqual(gap["accounts_with_history_gaps"], 1)
        self.assertFalse(gap["coverage_complete"])
        status["status"] = "error"
        failed_sync = reader.list(kind="spending", now=now)
        self.assertTrue(failed_sync["stale_or_partial"])
        self.assertEqual(failed_sync["matching_count"], 2)
        with self.assertRaisesRegex(Exception, "activity_filter_invalid"):
            reader.list(start="not-a-time")

    def test_rows_are_classified_from_the_real_feed_vocabulary(self):
        # (account_type, type, sub_type, amount_sign, amount) -> (category, direction, kind)
        cases = {
            ("ca_cash_msb", "SPEND", "PREPAID", "negative", "4.50"): ("purchase", "out", "spending"),
            ("ca_cash_msb", "SPEND", "PREPAID", "positive", "4.50"): ("refund", "in", "spending"),
            # Minus sign and negative sign disagree on direction: unknown, not a purchase,
            # but still listed with spending so a total can say it is unconfirmed.
            ("ca_cash_msb", "SPEND", "PREPAID", "negative", "-4.50"): ("other", "unknown", "spending"),
            ("ca_credit_card", "CREDIT_CARD", "PURCHASE", "negative", "9"): ("purchase", "out", "spending"),
            ("ca_credit_card", "CREDIT_CARD", "REFUND", "positive", "9"): ("refund", "in", "spending"),
            ("ca_credit_card", "CREDIT_CARD", "PAYMENT", "positive", "9"): ("card_payment", "in", "other"),
            ("ca_credit_card", "CREDIT_CARD", "INTEREST", "negative", "1"): ("other", "out", "other"),
            ("ca_cash_msb", "CREDIT_CARD_PAYMENT", None, "negative", "9"): ("card_payment", "out", "other"),
            ("ca_cash_msb", "WITHDRAWAL", "AFT", "negative", "9"): ("bill_payment", "out", "other"),
            ("ca_cash_msb", "WITHDRAWAL", "BILL_PAY", "negative", "9"): ("bill_payment", "out", "other"),
            ("ca_cash_msb", "WITHDRAWAL", "E_TRANSFER", "negative", "9"): ("transfer", "out", "other"),
            ("ca_cash_msb", "P2P_PAYMENT", "SEND", "negative", "9"): ("transfer", "out", "other"),
            ("ca_cash_msb", "REIMBURSEMENT", "CASHBACK", "positive", "1"): ("other", "in", "other"),
            ("tfsa", "DIY_BUY", "MARKET_ORDER", "positive", "9"): ("other", "unknown", "investment"),
            ("tfsa", "NON_RESIDENT_TAX", None, "negative", "1"): ("other", "out", "investment"),
            ("ca_cash_msb", "SOMETHING_NEW", None, "negative", "9"): ("other", "out", "other"),
        }
        for (account, kind, sub, sign, amount), expected in cases.items():
            self.assertEqual((activity_category(account, kind, sub, sign, amount),
                              activity_direction(kind, sign, amount),
                              activity_kind(account, kind, sub)),
                             expected, (account, kind, sub, sign, amount))
        self.assertEqual([activity_outcome(status) for status in
                          ("settled", "completed", "FILLED", "authorized", "pending",
                           "rejected", "CANCELLED", "expired", "reversed", None, "new")],
                         ["done", "done", "done", "pending", "pending",
                          "not_done", "not_done", "not_done", "not_done", "unknown", "unknown"])

    def test_rows_in_an_older_shape_are_reported_incomplete(self):
        status = {"status": "complete", "last_fetched_at": "2026-09-27T02:29:38+00:00"}
        old_row = {"activity_ref": "a", "occurred_at": "2026-09-26T20:00:00Z"}
        reader = ActivityReader("synthetic", "(default)", 25200,
                                db=FakeActivityDB(status, [old_row]))
        result = reader.list(now=datetime(2026, 9, 27, 3, 0, tzinfo=timezone.utc))
        self.assertEqual(result["matching_count"], 0)
        self.assertFalse(result["coverage_complete"])
        self.assertTrue(result["stale_or_partial"])

    def test_recent_sorts_by_time_not_by_string(self):
        rows = [self.saved({"activity_ref": "late", "account_type": "tfsa", "type": "DIY_BUY",
                            "occurred_at": "2026-09-26T21:00:00-04:00"}),
                self.saved({"activity_ref": "early", "account_type": "tfsa", "type": "DIY_BUY",
                            "occurred_at": "2026-09-26T23:00:00Z"})]
        reader = ActivityReader("synthetic", "(default)", 25200, db=FakeActivityDB({}, rows))
        self.assertEqual([row["activity_ref"] for row in reader.recent(3)], ["late", "early"])


class RealAuthorizationTests(unittest.TestCase):
    """Tool and owner-console behaviour, reusing the OAuth test harness."""
    setUp = test_oauth.OAuthTests.setUp
    register = test_oauth.OAuthTests.register
    begin = test_oauth.OAuthTests.begin
    consent = test_oauth.OAuthTests.consent
    get_code = test_oauth.OAuthTests.get_code
    exchange = test_oauth.OAuthTests.exchange
    tokens = test_oauth.OAuthTests.tokens
    mcp = test_oauth.OAuthTests.mcp

    def test_real_consent_and_tool_call(self):
        token = self.tokens()["access_token"]
        response = self.mcp(token)
        tools = {x["name"]: x for x in response.json()["result"]["tools"]}
        self.assertEqual(set(tools), {"get_data_status", "get_portfolio", "list_activities"})
        self.assertTrue(tools["get_data_status"]["annotations"]["readOnlyHint"])
        self.assertTrue(tools["get_portfolio"]["annotations"]["readOnlyHint"])
        self.assertTrue(tools["list_activities"]["annotations"]["readOnlyHint"])
        # Only the tools that may refresh from Wealthsimple reach outside this deployment.
        self.assertEqual({name: tool["annotations"]["openWorldHint"] for name, tool in tools.items()},
                         {"get_data_status": False, "get_portfolio": True, "list_activities": True})
        response = self.mcp(token, "tools/call", {"name": "get_data_status", "arguments": {}})
        result = response.json()["result"]
        self.assertFalse(result.get("isError", False), result)
        content = result.get("structuredContent") or json.loads(result["content"][0]["text"])
        self.assertTrue(content["portfolio"]["snapshot_available"])
        self.assertTrue(content["activities"]["coverage_complete"])
        self.assertEqual(content["owner_url"], BASE + "/owner")
        self.assertEqual(content["connector"]["version"], "dev")  # no version.json in tests
        self.assertNotIn("reconnect_url", content)
        response = self.mcp(token, "tools/call", {"name": "get_portfolio", "arguments": {}})
        result = response.json()["result"]
        self.assertFalse(result.get("isError", False), result)
        content = result.get("structuredContent") or json.loads(result["content"][0]["text"])
        self.assertEqual(content["positions"][0]["symbol"], "TEST")
        self.assertEqual(content["refresh"]["result"], "refresh_succeeded")
        self.assertEqual(self.reader.calls, 1)
        response = self.mcp(token, "tools/call", {"name": "list_activities",
                            "arguments": {"kind": "spending"}})
        result = response.json()["result"]
        self.assertFalse(result.get("isError", False), result)
        content = result.get("structuredContent") or json.loads(result["content"][0]["text"])
        self.assertEqual(content["kind"], "spending")
        self.assertEqual(content["read_mode"], "saved_activities")
        self.assertEqual(self.refresher.calls, ["portfolio", "activities"])
        self.assertEqual(self.refresher.forced, [])  # the AI reuses a fresh activity pass
        self.assertEqual(len(self.activity_reader.calls), 1)

    def test_reconnect_link_and_owner_page_without_new_tool(self):
        self.refresher.refresh = Mock(return_value={"result": "reconnect_required"})
        self.reader.status = Mock(return_value={
            "snapshot_available": True, "reconnect_required": True,
            "connection_state": "reconnect_required"})
        token = self.tokens()["access_token"]
        tools = self.mcp(token).json()["result"]["tools"]
        self.assertEqual({tool["name"] for tool in tools},
                         {"get_data_status", "get_portfolio", "list_activities"})
        response = self.mcp(token, "tools/call", {
            "name": "get_portfolio", "arguments": {}}).json()["result"]
        content = response.get("structuredContent") or json.loads(response["content"][0]["text"])
        self.assertEqual(content["refresh"]["reconnect_url"], BASE + "/owner")
        status = self.mcp(token, "tools/call", {
            "name": "get_data_status", "arguments": {}}).json()["result"]
        content = status.get("structuredContent") or json.loads(status["content"][0]["text"])
        self.assertEqual(content["reconnect_url"], BASE + "/owner")
        self.assertEqual(content["next_action"], "reconnect_wealthsimple")

    def call(self, token, name, arguments=None):
        response = self.mcp(token, "tools/call", {"name": name, "arguments": arguments or {}})
        result = response.json()["result"]
        self.assertFalse(result.get("isError"))
        return result.get("structuredContent") or json.loads(result["content"][0]["text"])

    def test_reconnect_guidance_survives_when_nothing_is_saved(self):
        self.refresher.refresh = Mock(return_value={
            "result": "reconnect_required", "error": "session_rotation_needs_recovery"})
        self.reader.portfolio = Mock(side_effect=SnapshotError("no_snapshot"))
        self.activity_reader.list = Mock(side_effect=ActivityError("no_activity_snapshot"))
        self.reader.status = Mock(return_value={
            "snapshot_available": False, "reconnect_required": True,
            "connection_state": "reconnect_required"})
        token = self.tokens()["access_token"]
        for tool, code in (("get_portfolio", "no_snapshot"),
                           ("list_activities", "no_activity_snapshot")):
            content = self.call(token, tool)
            self.assertFalse(content["data_available"])
            self.assertEqual(content["error_code"], code)
            self.assertEqual(content["refresh"], {
                "result": "reconnect_required", "error": "session_rotation_needs_recovery",
                "decision": "blocked", "reason": "session_rotation_needs_recovery",
                "reconnect_url": BASE + "/owner"})
            self.assertEqual(content["next_action"], "reconnect_wealthsimple")
            self.assertEqual(content["reconnect_url"], BASE + "/owner")

    def test_unavailable_data_says_what_to_do_next(self):
        self.activity_reader.list = Mock(side_effect=ActivityError("activity_filter_invalid"))
        token = self.tokens()["access_token"]
        self.assertEqual(self.call(token, "list_activities")["next_action"], "fix_arguments")
        self.refresher.refresh = Mock(return_value={"result": "sync_already_running"})
        self.reader.portfolio = Mock(side_effect=SnapshotError("no_snapshot"))
        content = self.call(token, "get_portfolio")
        self.assertEqual(content["next_action"], "retry_later")
        self.assertEqual(content["refresh"]["decision"], "in_progress")
        self.assertNotIn("reconnect_url", content)
        # A connected deployment with data answers normally, with nothing to do next.
        self.reader.portfolio = Mock(return_value={"accounts": []})
        content = self.call(token, "get_portfolio")
        self.assertTrue(content["data_available"])
        self.assertIsNone(content["next_action"])

    def test_refresh_says_whether_it_updated_the_data(self):
        token = self.tokens()["access_token"]
        self.refresher.refresh = Mock(return_value={
            "result": "refresh_reused", "pass_finished_at": "2026-09-27T12:00:00+00:00",
            "fresh_until": "2026-09-27T12:10:00+00:00"})
        refresh = self.call(token, "list_activities")["refresh"]
        self.assertEqual((refresh["decision"], refresh["reason"]), ("reused", "recent_pass"))
        self.assertEqual(refresh["fresh_until"], "2026-09-27T12:10:00+00:00")
        later = self.call(token, "list_activities", {"offset": 50})["refresh"]
        self.assertEqual((later["decision"], later["reason"]), ("skipped", "later_page"))
        self.refresher.refresh = Mock(return_value={"result": "refresh_failed",
                                                    "error": "rate_limited_stop"})
        failed = self.call(token, "get_portfolio")["refresh"]
        self.assertEqual((failed["decision"], failed["reason"]), ("failed", "rate_limited_stop"))

    def test_fresh_deployment_status_links_to_first_connection(self):
        self.reader.status = Mock(return_value={
            "snapshot_available": False, "reconnect_required": False,
            "connection_state": "not_connected"})
        token = self.tokens()["access_token"]
        status = self.mcp(token, "tools/call", {
            "name": "get_data_status", "arguments": {}}).json()["result"]
        content = status.get("structuredContent") or json.loads(status["content"][0]["text"])
        self.assertEqual(content["owner_url"], BASE + "/owner")
        self.assertEqual(content["reconnect_url"], BASE + "/owner")
        self.assertEqual(content["next_action"], "connect_wealthsimple")

    def test_owner_dashboard_requires_owner_and_never_returns_credentials(self):
        page = self.client.get("/owner")
        self.assertEqual(page.status_code, 200)
        self.assertIn("default-src 'none'", page.headers["content-security-policy"])
        self.assertEqual(page.headers["cache-control"], "no-store")
        self.assertIn('id="primary-action"', page.text)
        # The Wealthsimple sign-in form is hidden until status says it is needed.
        self.assertIn('id="connect-panel" class="panel connect hidden"', page.text)
        self.assertEqual(self.client.get("/assets/owner.js").status_code, 200)
        headers = {"Origin": BASE, "Authorization": "Bearer synthetic-owner-login"}
        self.assertEqual(self.client.post("/owner/status", json={},
                                         headers={"Origin": BASE}).status_code, 403)
        self.assertEqual(self.client.post("/owner/status", json={}, headers={
            "Origin": BASE, "Authorization": "Bearer synthetic-stranger-login"
        }).status_code, 403)
        status = self.client.post("/owner/status", json={}, headers=headers)
        self.assertEqual(status.status_code, 200, status.text)
        diagnostics = status.json()["diagnostics"]
        self.assertEqual(diagnostics["connector_version"], "dev")  # no version.json in tests
        self.assertEqual(diagnostics["mcp_endpoint"], BASE + "/mcp")
        # The overview renders holdings and recent activity from this one response.
        self.assertEqual(status.json()["positions"][0]["symbol"], "TEST")
        self.assertEqual(status.json()["recent_activities"][0]["asset_symbol"], "TEST")
        self.assertNotIn("NEVER-EXPORT", status.text)
        self.assertNotIn("refresh.example", status.text)
        self.assertNotIn("reconnect.example", status.text)
        refreshed = self.client.post("/owner/refresh", json={"target": "portfolio"},
                                     headers=headers)
        self.assertEqual(refreshed.status_code, 200, refreshed.text)
        self.assertEqual(refreshed.json()["decision"], "performed")
        self.assertEqual(self.refresher.forced, ["portfolio"])  # the owner's Sync forces
        reconnected = self.client.post("/owner/reconnect", json={
            "username": "owner@example.com", "password": "synthetic-password",
            "otp": "123456"}, headers=headers)
        self.assertEqual(reconnected.json(), {"result": "reconnect_succeeded"})
        signed_out = self.client.post("/owner/signout", json={}, headers=headers)
        self.assertEqual(signed_out.json(), {"result": "signed_out"})
        self.assertNotIn("synthetic-password", reconnected.text)
        self.assertEqual(self.client.post("/owner/status", json={}, headers={
            **headers, "Origin": "https://evil.example"}).status_code, 403)

    def test_owner_preview_and_connected_apps(self):
        headers = {"Origin": BASE, "Authorization": "Bearer synthetic-owner-login"}
        stranger = {"Origin": BASE, "Authorization": "Bearer synthetic-stranger-login"}
        apps = lambda: self.client.post("/owner/status", json={}, headers=headers).json()["apps"]
        for path, body in (("/owner/preview", {"target": "portfolio"}),
                           ("/owner/apps/disconnect", {"client_id": "x"})):
            self.assertEqual(self.client.post(path, json=body, headers=stranger).status_code, 403)
        preview = self.client.post("/owner/preview", json={"target": "portfolio"}, headers=headers)
        self.assertEqual(preview.json()["positions"][0]["symbol"], "TEST")
        self.assertEqual(self.reader.calls, 1)
        self.assertEqual(self.refresher.calls, [])  # a preview never triggers a sync
        preview = self.client.post("/owner/preview", json={"target": "activities"}, headers=headers)
        self.assertEqual(preview.json()["read_mode"], "saved_activities")
        self.assertEqual(self.client.post("/owner/preview", json={"target": "secrets"},
                                          headers=headers).status_code, 400)

        self.assertEqual(apps(), [])
        tokens = self.tokens()
        self.assertEqual(self.mcp(tokens["access_token"]).status_code, 200)
        connected = apps()
        self.assertEqual(len(connected), 1)
        family = next(k for k in self.store.data if k.startswith("grant_"))
        self.assertIsNotNone(connected[0]["last_used_at"])
        self.assertEqual(connected[0]["connected_at"], self.store.data[family]["connected_at"])
        # Grants saved before connected_at existed show the app's registration time.
        del self.store.data[family]["connected_at"]
        self.assertEqual(apps()[0]["connected_at"], connected[0]["registered_at"])
        self.assertNotIn("token", json.dumps(connected))
        client_id = connected[0]["client_id"]
        revoked = self.client.post("/owner/apps/disconnect", json={"client_id": client_id},
                                   headers=headers)
        self.assertEqual(revoked.json(), {"revoked_grants": 1})
        self.assertEqual(apps(), [])
        # The disconnected app's access token stops working immediately.
        self.assertEqual(self.mcp(tokens["access_token"]).status_code, 401)
