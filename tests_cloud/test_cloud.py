"""No real Keychain, Cloud credentials or network used by these tests."""
import copy
from datetime import timedelta
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "firebase" / "functions"))
sys.path.insert(0, str(ROOT / "src"))
import main as app
import portfolio_service as service_module
from wealthsimple_connector.core import client as ws


class FakeDB:
    def __init__(self):
        self.rows = {}
    def collection(self, name):
        return Query(self, name)
    def transaction(self, **kwargs):
        return Tx(self)
    def batch(self):
        return Tx(self)


OPERATORS = {"==": lambda a, b: a == b, ">": lambda a, b: a > b, "<": lambda a, b: a < b}


class Query:
    """Enough of a Firestore collection query for the service's own filters."""
    def __init__(self, db, name, filters=()):
        self.db, self.name, self.filters = db, name, filters
    def document(self, key):
        return Ref(self.db, self.name + "/" + key)
    def where(self, filter):
        return Query(self.db, self.name, self.filters + (filter,))
    def order_by(self, field, direction=None):
        return self
    def stream(self, **kwargs):
        refs = [Ref(self.db, key) for key in list(self.db.rows)
                if key.startswith(self.name + "/")]
        return [Mock(reference=ref, to_dict=ref.get().to_dict) for ref in refs
                if all(f.field_path in (row := ref.get().to_dict())
                       and OPERATORS[f.op_string](row[f.field_path], f.value)
                       for f in self.filters)]


class Ref:
    def __init__(self, db, name):
        self.db, self.name = db, name
        self.id = name.rsplit("/", 1)[-1]
    def get(self, **kwargs):
        return Mock(to_dict=lambda: copy.deepcopy(self.db.rows.get(self.name)))
    def set(self, value, merge=False):
        self.db.rows[self.name] = {**(self.db.rows.get(self.name, {}) if merge else {}),
                                   **copy.deepcopy(value)}


class Tx:
    def __init__(self, db):
        self.db, self.writes = db, []
    def set(self, ref, value, merge=False):
        self.writes.append((ref.name, copy.deepcopy(value), merge, False))
    def create(self, ref, value):
        self.writes.append((ref.name, copy.deepcopy(value), False, True))
    def delete(self, ref):
        self.writes.append((ref.name, None, False, False))
    def commit(self):
        for name, value, merge, create in self.writes:
            if value is None:
                self.db.rows.pop(name, None)
                continue
            if create and name in self.db.rows:
                raise AssertionError("snapshot overwritten")
            self.db.rows[name] = {**(self.db.rows.get(name, {}) if merge else {}), **value}


def transactional(func):
    def run(tx):
        result = func(tx)
        tx.commit()
        return result
    return run


def response(body, status=200):
    return Mock(status_code=status, text=json.dumps(body))


class CloudTests(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB()
        self.patches = [patch.object(app.service, "_db", return_value=self.db),
                        patch.object(service_module.firestore, "transactional", transactional)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_lease_serializes_manual_and_scheduled_requests(self):
        lease, _ = app.service._acquire_lease()
        with self.assertRaisesRegex(ws.PortfolioError, "sync_already_running"):
            app.service._acquire_lease()
        app.service._record_failure(lease, 'portfolio', "synthetic_error"); app.service._release_lease(lease)
        new_lease, _ = app.service._acquire_lease()
        self.assertNotEqual(lease, new_lease)

    def test_refresh_cooldown_applies_only_to_requested_refresh(self):
        lease, _ = app.service._acquire_lease(cooldown_seconds=600)
        app.service._record_failure(lease, 'portfolio', "synthetic_error"); app.service._release_lease(lease)
        with self.assertRaisesRegex(ws.PortfolioError, "refresh_cooldown"):
            app.service._acquire_lease(cooldown_seconds=600)
        app.service._acquire_lease()

    def test_pending_rotation_blocks_next_job_after_crash(self):
        lease, _ = app.service._acquire_lease()
        app.service._update_session_state(lease, {"rotation_pending": True})
        app.service._record_failure(lease, 'portfolio', "synthetic_error"); app.service._release_lease(lease)
        with self.assertRaisesRegex(ws.PortfolioError, "session_rotation_needs_recovery"):
            app.service._acquire_lease()

    def test_reconnect_bypasses_pending_rotation_and_releases_lease(self):
        self.db.rows["connectors/wealthsimple"] = {
            "session": {"rotation_pending": True, "reconnect_required": True}}
        reader = Mock()
        reader.reconnect_session.return_value = self.session()
        with patch.object(app.service, "_load_session", return_value=self.session()), \
                patch.object(app.service, "_save_session") as save, \
                patch.object(service_module, "WealthsimpleReader", return_value=reader):
            result = app.service.reconnect("owner@example.com", "synthetic-password")
        self.assertEqual(result, {"result": "reconnect_succeeded"})
        save.assert_called_once()
        reader.reconnect_session.assert_called_once_with(
            "owner@example.com", "synthetic-password", None)
        self.assertIsNone(self.db.rows["connectors/wealthsimple"]["sync"]["lease_id"])

    def test_keep_alive_only_rotates_the_session(self):
        reader = Mock()
        reader.refresh_session.return_value = self.session()
        with patch.object(app.service, "_load_session", return_value=self.session()), \
                patch.object(app.service, "_save_session") as save, \
                patch.object(service_module, "WealthsimpleReader", return_value=reader):
            app.service.keep_session_alive()
        save.assert_called_once()
        reader.collect.assert_not_called()
        self.assertEqual(self.db.rows["connectors/wealthsimple"]["keep_alive"]["status"], "ok")
        self.assertIsNone(self.db.rows["connectors/wealthsimple"]["sync"]["lease_id"])
        # A refresh token Wealthsimple no longer accepts asks the owner to reconnect.
        self.db.rows["connectors/wealthsimple"]["session"]["rotation_pending"] = False  # save was mocked
        reader.refresh_session.side_effect = ws.PortfolioError("refresh_failed_reauth_may_be_required")
        with patch.object(app.service, "_load_session", return_value=self.session()), \
                patch.object(service_module, "WealthsimpleReader", return_value=reader):
            with self.assertRaises(ws.PortfolioError):
                app.service.keep_session_alive()
        state = self.db.rows["connectors/wealthsimple"]
        self.assertTrue(state["session"]["reconnect_required"])
        self.assertEqual(state["keep_alive"]["error_code"], "refresh_failed_reauth_may_be_required")
        self.assertNotIn("portfolio", state)  # the saved portfolio did not fail

    def test_first_reconnect_bootstraps_an_empty_cloud_secret(self):
        reader = Mock()
        reader.bootstrap_session.return_value = self.session()
        reader.reconnect_session.return_value = self.session()
        with patch.object(app.service, "_load_session",
                          side_effect=service_module.NotFound("empty secret")), \
                patch.object(app.service, "_save_session") as save, \
                patch.object(service_module, "WealthsimpleReader", return_value=reader):
            result = app.service.reconnect("owner@example.com", "synthetic-password")
        self.assertEqual(result, {"result": "reconnect_succeeded"})
        reader.bootstrap_session.assert_called_once_with()
        save.assert_called_once()

    def test_expired_worker_cannot_change_pointer_or_publish_snapshot(self):
        lease, _ = app.service._acquire_lease()
        self.db.rows['connectors/wealthsimple']['sync']['lease_until'] = app.service._now() - timedelta(seconds=1)
        with self.assertRaisesRegex(ws.PortfolioError, "sync_lease_lost"):
            app.service._update_session_state(lease, {"secret_version": "fake"})
        with self.assertRaisesRegex(ws.PortfolioError, "sync_lease_lost"):
            app.service._save_snapshot(lease, self.snapshot())
        self.assertFalse(any(k.startswith('portfolio_snapshots/') for k in self.db.rows))

    @staticmethod
    def snapshot():
        return {"fetched_at": app.service._now().isoformat(), "accounts": [], "positions": [],
                "valuation_as_of": None, "nested": [["allowed JSON blob"]]}

    def test_snapshot_history_and_head_saved_atomically(self):
        lease, _ = app.service._acquire_lease()
        snapshot = self.snapshot()
        app.service._save_snapshot(lease, snapshot)
        state = self.db.rows['connectors/wealthsimple']
        snapshot_id = state['portfolio']['current_snapshot_id']
        self.assertEqual(json.loads(self.db.rows['portfolio_snapshots/' + snapshot_id]['payload_json']), snapshot)
        self.assertEqual(len([k for k in self.db.rows if k.startswith('portfolio_snapshots/')]), 1)
        self.assertIsNone(state['sync']['lease_id'])

    def test_failed_sync_retains_snapshot_but_reports_stale(self):
        lease, _ = app.service._acquire_lease()
        app.service._save_snapshot(lease, self.snapshot())
        snapshot_keys = [key for key in self.db.rows if key.startswith('portfolio_snapshots/')]
        lease, _ = app.service._acquire_lease()
        app.service._record_failure(lease, 'portfolio', 'rate_limited_stop'); app.service._release_lease(lease)
        self.assertEqual([key for key in self.db.rows if key.startswith('portfolio_snapshots/')],
                         snapshot_keys)
        self.assertEqual(self.db.rows['connectors/wealthsimple']['portfolio']['status'], 'error')
        self.assertEqual(self.db.rows['connectors/wealthsimple']['portfolio']['error_code'],
                         'rate_limited_stop')

    def test_activity_sync_backfills_every_page_and_includes_closed_account(self):
        accounts = [{'id': 'open-cash', 'type': 'ca_cash_msb', 'status': 'open'},
                    {'id': 'old-card', 'type': 'ca_credit_card', 'status': 'closed'}]
        reader = Mock()
        reader.activity_accounts.return_value = accounts

        def page(account_id, window_start, window_end, cursor):
            number = int(cursor or '0')
            node = {'accountId': account_id, 'canonicalId': f'synthetic-{number}',
                    'occurredAt': f'2020-01-{number + 1:02}T12:00:00Z',
                    'type': 'CREDIT_CARD', 'status': 'settled'}
            return [node], (str(number + 1) if account_id == 'open-cash' and number < 4
                            else None)

        with patch.object(app.service, '_load_session', return_value=self.session()), \
                patch.object(service_module, 'WealthsimpleReader', return_value=reader):
            reader.activity_page.side_effect = page
            result = app.service.sync_activity()
        self.assertEqual(result['result'], 'refresh_succeeded')
        self.assertEqual(reader.activity_page.call_count, 6)
        status = self.db.rows['connectors/wealthsimple']['activities']
        self.assertEqual(status['status'], 'complete')
        self.assertEqual(status['account_index'], 2)
        self.assertEqual(status['pages_fetched'], 6)
        self.assertEqual(len([k for k in self.db.rows if k.startswith('activities/')]), 6)
        self.assertEqual({k.split('/', 1)[0] for k in self.db.rows},
                         {'connectors', 'activities'})
        self.assertNotIn('old-card', json.dumps(result))

    def test_long_activity_sync_stops_in_time_and_continues_where_it_left_off(self):
        accounts = [{'id': 'open-cash', 'type': 'ca_cash_msb', 'status': 'open'}]
        reader = Mock()
        reader.activity_accounts.return_value = accounts

        def page(account_id, window_start, window_end, cursor):
            number = int(cursor or '0')
            node = {'accountId': account_id, 'canonicalId': f'synthetic-{number}',
                    'occurredAt': f'2020-01-{number + 1:02}T12:00:00Z',
                    'type': 'CREDIT_CARD', 'status': 'settled'}
            return [node], (str(number + 1) if number < 4 else None)

        reader.activity_page.side_effect = page
        clock = iter([0, 10, 200])  # start, first page quick, second page past the budget
        with patch.object(app.service, '_load_session', return_value=self.session()), \
                patch.object(service_module, 'WealthsimpleReader', return_value=reader), \
                patch.object(service_module.time, 'monotonic', side_effect=lambda: next(clock)):
            first = app.service.sync_activity()
        self.assertEqual(first['result'], 'refresh_continues')
        self.assertEqual(first['rows_processed'], 2)
        self.assertIsNone(self.db.rows['connectors/wealthsimple']['sync']['lease_id'])
        with patch.object(app.service, '_load_session', return_value=self.session()), \
                patch.object(service_module, 'WealthsimpleReader', return_value=reader):
            second = app.service.sync_activity()
        self.assertEqual(second['result'], 'refresh_succeeded')
        self.assertEqual(second['rows_processed'], 5)
        self.assertEqual(reader.activity_page.call_count, 5)  # no page fetched twice

    def test_sign_out_destroys_the_session_and_blocks_syncs_until_reconnect(self):
        sm = Mock()
        sm.list_secret_versions.return_value = [SimpleNamespace(name='projects/p/secrets/s/versions/7')]
        with patch.object(service_module.secretmanager, 'SecretManagerServiceClient', return_value=sm), \
                patch.object(app.service, '_project_id', return_value='fake-project'):
            self.assertEqual(app.service.sign_out(), {'result': 'signed_out'})
        sm.destroy_secret_version.assert_called_once_with(
            request={'name': 'projects/p/secrets/s/versions/7'}, timeout=15)
        state = self.db.rows['connectors/wealthsimple']
        self.assertTrue(state['session']['signed_out'])
        self.assertIsNone(state['session']['secret_version'])
        self.assertIsNone(state['sync']['lease_id'])
        with self.assertRaisesRegex(ws.PortfolioError, 'signed_out'):
            app.service.sync()
        # Signing in again starts from an empty session, since every stored copy is gone.
        reader = Mock()
        reader.bootstrap_session.return_value = self.session()
        reader.reconnect_session.return_value = self.session()
        with patch.object(app.service, '_load_session',
                          side_effect=service_module.FailedPrecondition('destroyed')), \
                patch.object(app.service, '_save_session') as save, \
                patch.object(service_module, 'WealthsimpleReader', return_value=reader):
            self.assertEqual(app.service.reconnect('owner@example.com', 'synthetic-password'),
                             {'result': 'reconnect_succeeded'})
        reader.bootstrap_session.assert_called_once_with()
        save.assert_called_once()

    def test_expired_worker_cannot_save_activity(self):
        reader = Mock()
        reader.activity_accounts.return_value = [
            {'id': 'cash', 'type': 'ca_cash_msb', 'status': 'open'}]

        def page(*args):
            # Another worker takes over while this one is still reading.
            self.db.rows['connectors/wealthsimple']['sync']['lease_id'] = 'someone-else'
            return [{'accountId': 'cash', 'canonicalId': 'synthetic-1',
                     'occurredAt': '2020-01-01T12:00:00Z', 'type': 'SPEND'}], None

        reader.activity_page.side_effect = page
        with patch.object(app.service, '_load_session', return_value=self.session()), \
                patch.object(service_module, 'WealthsimpleReader', return_value=reader):
            with self.assertRaisesRegex(ws.PortfolioError, 'sync_lease_lost'):
                app.service.sync_activity()
        self.assertFalse(any(k.startswith('activities/') for k in self.db.rows))

    def test_activity_failure_is_recorded_and_releases_the_lease(self):
        reader = Mock()
        reader.activity_accounts.side_effect = ws.AccessTokenExpired()
        reader.refresh_session.return_value = self.session()
        with patch.object(app.service, '_load_session', return_value=self.session()), \
                patch.object(app.service, '_save_session'), \
                patch.object(service_module, 'WealthsimpleReader', return_value=reader):
            with self.assertRaisesRegex(ws.PortfolioError, 'reauth_required'):
                app.service.sync_activity()
        state = self.db.rows['connectors/wealthsimple']
        self.assertEqual(state['activities']['error_code'], 'reauth_required')
        self.assertTrue(state['session']['reconnect_required'])
        self.assertIsNone(state['sync']['lease_id'])
        reader.close.assert_called_once()

    def sync_activity_with(self, accounts, pages):
        """Run one activity sync where pages maps an account id to the nodes it returns."""
        reader = Mock()
        reader.activity_accounts.return_value = accounts
        reader.activity_page.side_effect = lambda account_id, *args: (pages[account_id], None)
        with patch.object(app.service, '_load_session', return_value=self.session()), \
                patch.object(service_module, 'WealthsimpleReader', return_value=reader):
            result = app.service.sync_activity()
        return result, reader

    def saved_activities(self):
        return {row['sub_type']: row for key, row in self.db.rows.items()
                if key.startswith('activities/')}

    def test_activity_pass_rereads_overlap_and_fixes_changed_rows(self):
        cash = [{'id': 'cash', 'type': 'ca_cash_msb', 'status': 'open'}]
        recent = (app.service._now() - timedelta(days=1)).isoformat()

        def node(canonical, status):
            return {'accountId': 'cash', 'canonicalId': canonical, 'occurredAt': recent,
                    'type': 'SPEND', 'subType': canonical, 'status': status}

        self.sync_activity_with(cash, {'cash': [node('purchase', 'pending'),
                                                node('hold', 'authorized')]})
        first_end = self.db.rows['connectors/wealthsimple']['activities']['window_end']
        # The purchase settles and the hold is released, both after the first pass ended.
        result, reader = self.sync_activity_with(cash, {'cash': [node('purchase', 'settled')]})
        self.assertEqual(result['result'], 'refresh_succeeded')
        start = reader.activity_page.call_args.args[1]
        self.assertEqual(service_module._utc(start),
                         service_module._utc(first_end) - timedelta(days=30))
        saved = self.saved_activities()
        self.assertEqual(saved['purchase']['status'], 'settled')
        self.assertNotIn('hold', saved)

    def test_unreadable_row_keeps_the_rows_saved_before(self):
        cash = [{'id': 'cash', 'type': 'ca_cash_msb', 'status': 'open'}]
        recent = (app.service._now() - timedelta(days=1)).isoformat()
        self.sync_activity_with(cash, {'cash': [
            {'accountId': 'cash', 'canonicalId': 'kept', 'occurredAt': recent, 'subType': 'kept'}]})
        # The same row comes back without its time: it may be the saved one, so keep it.
        result, _ = self.sync_activity_with(cash, {'cash': [
            {'accountId': 'cash', 'canonicalId': 'kept'}]})
        self.assertEqual(result['result'], 'refresh_partial')
        self.assertIn('kept', self.saved_activities())

    def test_history_gap_stays_until_its_range_is_read_cleanly(self):
        cash = [{'id': 'cash', 'type': 'ca_cash_msb', 'status': 'open'}]
        old = {'accountId': 'cash', 'canonicalId': 'old', 'occurredAt': '2021-03-01T12:00:00Z'}
        unreadable = {'accountId': 'cash', 'canonicalId': 'broken'}  # no time
        first, _ = self.sync_activity_with(cash, {'cash': [old, unreadable]})
        self.assertEqual(first['result'], 'refresh_partial')
        # A clean recent read must not hide the old gap: the whole range is read again.
        second, reader = self.sync_activity_with(cash, {'cash': [old, unreadable]})
        self.assertEqual(reader.activity_page.call_args.args[1], service_module.BACKFILL_START)
        self.assertEqual(second['result'], 'refresh_partial')
        self.assertFalse(second['coverage_complete'])
        # Once that range reads cleanly, coverage is complete and reads are incremental again.
        third, _ = self.sync_activity_with(cash, {'cash': [old]})
        self.assertEqual(third['result'], 'refresh_succeeded')
        self.assertTrue(third['coverage_complete'])
        _, reader = self.sync_activity_with(cash, {'cash': []})
        self.assertNotEqual(reader.activity_page.call_args.args[1], service_module.BACKFILL_START)

    def connected(self, **session):
        self.db.rows.setdefault('connectors/wealthsimple', {})['session'] = {
            'secret_version': 'projects/p/secrets/s/versions/1', **session}

    def test_consecutive_activity_reads_reuse_a_fresh_pass(self):
        cash = [{'id': 'cash', 'type': 'ca_cash_msb', 'status': 'open'}]
        self.connected()
        self.sync_activity_with(cash, {'cash': []})
        # "Spending", then "investing", then "this account": one sync answers all three.
        for _ in range(3):
            result, reader = self.sync_activity_with(cash, {'cash': []})
            self.assertEqual(result['result'], 'refresh_reused')
            self.assertTrue(result['coverage_complete'])
            self.assertIn('fresh_until', result)
            reader.activity_accounts.assert_not_called()
        # The dashboard's Sync forces a new pass.
        reader = Mock()
        reader.activity_accounts.return_value = cash
        reader.activity_page.return_value = ([], None)
        with patch.object(app.service, '_load_session', return_value=self.session()), \
                patch.object(service_module, 'WealthsimpleReader', return_value=reader):
            forced = app.service.sync_activity(force=True)
        self.assertEqual(forced['result'], 'refresh_succeeded')
        reader.activity_page.assert_called_once()

    def test_stale_or_unfinished_activity_is_synced_again(self):
        cash = [{'id': 'cash', 'type': 'ca_cash_msb', 'status': 'open'}]
        self.connected()
        self.sync_activity_with(cash, {'cash': []})
        activities = self.db.rows['connectors/wealthsimple']['activities']
        activities['pass_finished_at'] = (app.service._now() - timedelta(minutes=11)).isoformat()
        stale, _ = self.sync_activity_with(cash, {'cash': []})
        self.assertEqual(stale['result'], 'refresh_succeeded')
        # A backfill still in progress keeps going instead of waiting out the window.
        self.db.rows['connectors/wealthsimple']['activities']['status'] = 'running'
        running, reader = self.sync_activity_with(cash, {'cash': []})
        reader.activity_accounts.assert_called_once()
        self.assertNotEqual(running['result'], 'refresh_reused')

    def test_reused_pass_still_reports_incomplete_coverage(self):
        cash = [{'id': 'cash', 'type': 'ca_cash_msb', 'status': 'open'}]
        self.connected()
        self.sync_activity_with(cash, {'cash': [{'accountId': 'cash', 'canonicalId': 'broken'}]})
        reused, _ = self.sync_activity_with(cash, {'cash': []})
        self.assertEqual(reused['result'], 'refresh_reused')
        self.assertFalse(reused['coverage_complete'])

    def test_fresh_pass_does_not_hide_a_needed_reconnect(self):
        cash = [{'id': 'cash', 'type': 'ca_cash_msb', 'status': 'open'}]
        self.connected()
        self.sync_activity_with(cash, {'cash': []})
        self.connected(rotation_pending=True)  # a rotation no sync is finishing
        request = Mock(method='POST', get_json=Mock(return_value={'target': 'activities'}))
        result = json.loads(app.request_refresh(request).get_data())
        self.assertEqual(result['result'], 'reconnect_required')

    def test_new_account_backfills_alone(self):
        cash = {'id': 'cash', 'type': 'ca_cash_msb', 'status': 'open'}
        card = {'id': 'card', 'type': 'ca_credit_card', 'status': 'open'}
        self.sync_activity_with([cash], {'cash': []})
        _, reader = self.sync_activity_with([card, cash], {'cash': [], 'card': []})
        starts = {c.args[0]: c.args[1] for c in reader.activity_page.call_args_list}
        self.assertEqual(starts['card'], service_module.BACKFILL_START)
        self.assertNotEqual(starts['cash'], service_module.BACKFILL_START)

    def test_rows_saved_in_an_older_shape_are_read_again_in_full(self):
        cash = [{'id': 'cash', 'type': 'ca_cash_msb', 'status': 'open'}]
        self.db.rows['connectors/wealthsimple'] = {'activities': {
            'status': 'complete', 'window_end': '2026-01-01T00:00:00+00:00',
            'account_index': 1, 'cursor': None, 'pages_fetched': 1, 'rows_seen': 0,
            'skipped_count': 0, 'sync_start': '2025-12-01T00:00:00+00:00'}}
        _, reader = self.sync_activity_with(cash, {'cash': []})
        self.assertEqual(reader.activity_page.call_args.args[1], service_module.BACKFILL_START)

    def test_secret_number_normalized_and_rotation_pending_cleared_only_on_save(self):
        lease, _ = app.service._acquire_lease()
        sm = Mock()
        sm.access_secret_version.return_value = SimpleNamespace(name='projects/123/secrets/wealthsimple-session/versions/3',
            payload=Mock(data=self.session().to_json().encode()))
        sm.add_secret_version.return_value = SimpleNamespace(name='projects/123/secrets/wealthsimple-session/versions/4')
        with patch.object(service_module.secretmanager, 'SecretManagerServiceClient', return_value=sm), \
                patch.object(app.service, '_project_id', return_value='fake-project'):
            session = app.service._load_session(lease, None)
            app.service._update_session_state(lease, {'rotation_pending': True})
            self.assertTrue(self.db.rows['connectors/wealthsimple']['session']['rotation_pending'])
            app.service._save_session(lease, session)
        state = self.db.rows['connectors/wealthsimple']['session']
        self.assertFalse(state['rotation_pending'])
        self.assertEqual(state['secret_version'], 'projects/fake-project/secrets/wealthsimple-session/versions/4')
        # The spent version is destroyed so enabled (billed) versions don't pile up.
        sm.destroy_secret_version.assert_called_once_with(
            request={'name': 'projects/fake-project/secrets/wealthsimple-session/versions/3'}, timeout=15)
        self.assertNotIn('fake-access', json.dumps(state, default=str))

    def test_refresh_timeout_asks_for_reconnect_until_one_succeeds(self):
        sm = Mock()
        sm.add_secret_version.return_value = SimpleNamespace(
            name='projects/123/secrets/wealthsimple-session/versions/5')
        reader = Mock()
        reader.collect.side_effect = ws.AccessTokenExpired()
        reader.refresh_session.side_effect = TimeoutError()  # was the refresh token spent?
        request = Mock(method='POST', get_json=Mock(return_value={'target': 'portfolio'}))
        with patch.object(app.service, '_load_session', return_value=self.session()), \
                patch.object(service_module, 'WealthsimpleReader', return_value=reader), \
                patch.object(service_module.secretmanager, 'SecretManagerServiceClient',
                             return_value=sm), \
                patch.object(app.service, '_project_id', return_value='fake-project'):
            first = json.loads(app.request_refresh(request).get_data())
            state = self.db.rows['connectors/wealthsimple']
            self.assertEqual(first, {'result': 'reconnect_required',
                                     'error': 'session_rotation_needs_recovery'})
            self.assertEqual(state['portfolio']['error_code'], 'cloud_dependency_failed')
            self.assertTrue(state['session']['reconnect_required'])
            # Syncing again cannot recover, and says so the same way.
            again = json.loads(app.request_refresh(request).get_data())
            self.assertEqual(again['result'], 'reconnect_required')
            # Signing in again clears the recovery state, and syncs work.
            reader.reconnect_session.return_value = self.session()
            self.assertEqual(app.service.reconnect('owner@example.com', 'synthetic-password'),
                             {'result': 'reconnect_succeeded'})
            session = self.db.rows['connectors/wealthsimple']['session']
            self.assertFalse(session['rotation_pending'])
            self.assertFalse(session['reconnect_required'])
            reader.collect.side_effect = None
            reader.collect.return_value = self.snapshot()
            self.db.rows['connectors/wealthsimple']['portfolio'].pop('last_refresh_requested_at')
            third = json.loads(app.request_refresh(request).get_data())
        self.assertEqual(third['result'], 'refresh_succeeded')

    def test_secret_failure_preserves_pending_marker(self):
        lease, _ = app.service._acquire_lease()
        with patch.object(service_module.secretmanager, 'SecretManagerServiceClient') as sm, \
                patch.object(app.service, '_project_id', return_value='fake-project'):
            app.service._update_session_state(lease, {'rotation_pending': True})
            sm.return_value.add_secret_version.side_effect = RuntimeError('synthetic')
            with self.assertRaises(RuntimeError):
                app.service._save_session(lease, self.session())
        self.assertTrue(self.db.rows['connectors/wealthsimple']['session']['rotation_pending'])

    @staticmethod
    def session():
        return ws.Session(client_id='c', access_token='fake-access', refresh_token='r',
                          session_id='s', wssdi='d')

    def test_refresh_errors_become_one_result_vocabulary(self):
        self.assertEqual(app._refresh_outcome('reauth_required'),
                         {'result': 'reconnect_required', 'error': 'reauth_required'})
        self.assertEqual(app._refresh_outcome('refresh_cooldown'), {'result': 'refresh_cooldown'})
        self.assertEqual(app._refresh_outcome('graphql_errors_PortfolioPositions'),
                         {'result': 'refresh_failed', 'error': 'graphql_errors_PortfolioPositions'})

    def test_http_functions_are_private_and_use_sync_identity(self):
        refresh = app.request_refresh.__firebase_endpoint__
        reconnect = app.reconnect_now.__firebase_endpoint__
        self.assertEqual(refresh.httpsTrigger, {'invoker': ['private']})
        self.assertEqual(reconnect.httpsTrigger, {'invoker': ['private']})
        self.assertEqual(refresh.serviceAccountEmail, reconnect.serviceAccountEmail)
        self.assertFalse(hasattr(app, 'sync_now'))
        self.assertFalse(hasattr(app, 'sync_activity_now'))
        self.assertFalse(hasattr(app, 'portfolio_api'))


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.session = ws.Session(client_id='c', access_token='a', refresh_token='r', session_id='s', wssdi='d')
        self.reader = ws.WealthsimpleReader(self.session)
        self.reader.http.close()
        self.reader.http = Mock()

    def test_first_session_bootstrap_uses_only_allowlisted_public_assets(self):
        page = Mock(status_code=200, text=(
            '<script src="https://assets.wealthsimple.com/app-abcdef0123.js"></script>'))
        page.cookies = {"wssdi": "abcdef01-2345-6789-abcd-ef0123456789"}
        bundle = Mock(status_code=200, text='"production":{clientId:"abcdef0123"}')
        self.reader.http.request.side_effect = [page, bundle]
        session = self.reader.bootstrap_session()
        self.assertEqual(session.client_id, "abcdef0123")
        self.assertEqual(session.wssdi, page.cookies["wssdi"])
        self.assertRegex(session.session_id, r"^[0-9a-f-]{36}$")
        for call in self.reader.http.request.call_args_list:
            self.assertEqual(call.kwargs["headers"], {})
            self.assertFalse(call.kwargs["allow_redirects"])

    def test_first_session_bootstrap_rejects_an_untrusted_bundle(self):
        page = Mock(status_code=200,
                    text='<script src="https://example.com/app-abcdef.js"></script>')
        page.cookies = {"wssdi": "abcdef01-2345-6789-abcd-ef0123456789"}
        self.reader.http.request.return_value = page
        with self.assertRaisesRegex(ws.PortfolioError, "bundle_untrusted"):
            self.reader.bootstrap_session()
        self.assertEqual(self.reader.http.request.call_count, 1)

    def test_refresh_rotates_in_memory_without_password(self):
        self.reader.http.request.return_value = response({'access_token':'new-a', 'refresh_token':'new-r'})
        session = self.reader.refresh_session()
        self.assertEqual(session.refresh_token, 'new-r')
        req = self.reader.http.request.call_args.kwargs
        self.assertNotIn('Authorization', req['headers'])
        self.assertNotIn('password', req['json'])

    def test_reconnect_uses_fixed_password_grant_and_validates_scopes(self):
        self.reader.http.request.side_effect = [
            response({'access_token': 'new-a', 'refresh_token': 'new-r'}),
            response({'scope': 'invest.read trade.read',
                      'identity_canonical_id': 'identity'}),
        ]
        session = self.reader.reconnect_session(
            'owner@example.com', 'synthetic-password', '123456')
        self.assertEqual(session.access_token, 'new-a')
        request = self.reader.http.request.call_args_list[0].kwargs
        self.assertEqual(request['json']['grant_type'], 'password')
        self.assertEqual(request['json']['scope'], 'invest.read trade.read')
        self.assertEqual(request['headers']['x-wealthsimple-otp'], '123456;remember=true')

    def test_reconnect_prompts_for_mfa_without_exposing_upstream_message(self):
        self.reader.http.request.return_value = response(
            {'error': 'invalid_grant', 'error_description': 'PRIVATE'}, 401)
        with self.assertRaisesRegex(ws.PortfolioError, 'mfa_required') as raised:
            self.reader.reconnect_session('owner@example.com', 'synthetic-password')
        self.assertIsNone(raised.exception.method)

    def test_reconnect_says_where_the_code_was_sent(self):
        challenge = response({'error': 'invalid_grant'}, 401)
        challenge.headers = {'x-wealthsimple-otp': 'required; method=sms; digits=1234'}
        self.reader.http.request.return_value = challenge
        with self.assertRaises(ws.MfaRequired) as raised:
            self.reader.reconnect_session('owner@example.com', 'synthetic-password')
        self.assertEqual((raised.exception.method, raised.exception.hint), ('sms', '1234'))

    def test_otp_challenge_keeps_only_known_methods_and_phone_digits(self):
        def read(header):
            error = ws.otp_challenge(header)
            return error.method, error.hint
        self.assertEqual(read('required; method=app; digits=6'), ('app', None))
        self.assertEqual(read('required; method=email; digits=o***@example.com'), ('email', None))
        self.assertEqual(read('required; method=sms; digits=6'), ('sms', None))
        self.assertEqual(read('required; method=sms; digits=<b>12</b>'), ('sms', None))
        self.assertEqual(read('required; method=push'), (None, None))
        self.assertEqual(read(None), (None, None))

    def test_reconnect_function_returns_the_code_method(self):
        request = Mock(method='POST', get_json=Mock(return_value={
            'username': 'owner@example.com', 'password': 'synthetic-password'}))
        with patch.object(app.service, 'reconnect', side_effect=ws.MfaRequired('sms', '1234')):
            result = json.loads(app.reconnect_now(request).get_data())
        self.assertEqual(result, {'result': 'mfa_required', 'method': 'sms', 'hint': '1234'})

    def test_partial_graphql_error_is_not_a_snapshot(self):
        self.reader.http.request.return_value = response({'data':{'accounts':[]},'errors':[{'message':'SECRET'}]})
        with self.assertRaisesRegex(ws.PortfolioError, 'graphql_errors_PortfolioBalances'):
            self.reader._balances_batch([])

    def test_deadline_stops_requests(self):
        self.reader.deadline = time.monotonic() - 1
        with self.assertRaisesRegex(ws.PortfolioError, 'sync_deadline_reached'):
            self.reader.collect()
        self.reader.http.request.assert_not_called()

    def test_reader_has_no_arbitrary_query_interface(self):
        self.assertFalse(hasattr(self.reader, 'query'))

    def test_scope_mismatch_rejected(self):
        self.reader.http.request.return_value = response({'scope':'invest.read trade.read trade.write'})
        with self.assertRaisesRegex(ws.PortfolioError, 'granted_scopes_differ_from_request'):
            self.reader.collect()

    def test_collect_reads_the_three_fixed_queries(self):
        self.reader.http.request.side_effect = [
            response({'scope':'invest.read trade.read', 'identity_canonical_id':'identity'}),
            response({'data': {'identity': {'accounts': {
                'edges': [{'node': {'id': 'account'}}],
                'pageInfo': {'hasNextPage': False, 'endCursor': None},
            }}}}),
            response({'data': {'identity': {'financials': {'current': {'positions': {
                'edges': [{'node': {'id': 'position'}}],
                'pageInfo': {'hasNextPage': False, 'endCursor': None},
                'totalCount': 1,
            }}}}}}),
            response({'data': {'accounts': [{'id': 'account'}]}}),
        ]

        snapshot = self.reader.collect()

        self.assertEqual([a['id'] for a in snapshot['accounts']], ['account'])
        self.assertEqual([p['id'] for p in snapshot['positions']], ['position'])
        self.assertEqual(snapshot['trading_balances'], [{'id': 'account'}])
        operations = [call.kwargs['json']['operationName']
                      for call in self.reader.http.request.call_args_list[1:]]
        self.assertEqual(operations, ['PortfolioAccounts', 'PortfolioPositions', 'PortfolioBalances'])

    def test_activity_accounts_include_closed_supported_accounts(self):
        accounts = [
            {'id': 'cash', 'type': 'ca_cash_msb', 'status': 'open'},
            {'id': 'card', 'type': 'ca_credit_card', 'status': 'closed'},
            {'id': 'unsupported', 'type': 'other', 'status': 'open'},
        ]
        with patch.object(self.reader, '_identity_id', return_value='identity'), \
                patch.object(self.reader, '_collect_accounts', return_value=(accounts, [])):
            result = self.reader.activity_accounts()
        self.assertEqual([row['id'] for row in result], ['card', 'cash'])

    def test_activity_page_uses_requested_window_and_validates_account(self):
        node = {'accountId': 'old-card', 'canonicalId': 'synthetic-id',
                'occurredAt': '2017-01-01T00:00:00Z'}
        with patch.object(self.reader, '_graphql', return_value={
            'activityFeedItems': {'edges': [{'node': node}],
                                  'pageInfo': {'hasNextPage': False, 'endCursor': None}}
        }) as graphql:
            rows, cursor = self.reader.activity_page(
                'old-card', '2016-01-01T00:00:00Z', '2026-09-27T00:00:00Z')
        self.assertEqual(rows, [node])
        self.assertIsNone(cursor)
        condition = graphql.call_args.args[2]['condition']
        self.assertEqual(condition['startDate'], '2016-01-01T00:00:00Z')
        self.assertEqual(condition['endDate'], '2026-09-27T00:00:00Z')

    def test_cloud_service_refreshes_once_then_restarts_the_read(self):
        events = []
        snapshot = {"fetched_at": "2026-01-01T00:00:00+00:00", "accounts": [], "positions": []}
        reader = Mock()

        def collect(_currency):
            events.append("collect")
            if events.count("collect") == 1:
                raise ws.AccessTokenExpired()
            return snapshot

        reader.collect.side_effect = collect
        reader.refresh_session.side_effect = lambda: events.append("refresh") or self.session

        service = app.service
        with patch.object(service, '_acquire_lease', return_value=('lease', None)), \
                patch.object(service, '_load_session', return_value=self.session), \
                patch.object(service, '_update_session_state', side_effect=lambda _lease, _values: events.append('begin')), \
                patch.object(service, '_release_lease'), \
                patch.object(service, '_save_session', side_effect=lambda _lease, _session: events.append('save_session')), \
                patch.object(service, '_save_snapshot', side_effect=lambda _lease, _snapshot: events.append('save_snapshot')), \
                patch.object(service_module, 'WealthsimpleReader', return_value=reader):
            service.sync()

        self.assertEqual(events, ['collect', 'begin', 'refresh', 'save_session', 'collect', 'save_snapshot'])

    def test_cloud_service_does_not_refresh_twice(self):
        reader = Mock()
        reader.collect.side_effect = ws.AccessTokenExpired()
        reader.refresh_session.return_value = self.session
        service = app.service

        with patch.object(service, '_acquire_lease', return_value=('lease', None)), \
                patch.object(service, '_load_session', return_value=self.session), \
                patch.object(service, '_update_session_state'), \
                patch.object(service, '_save_session'), \
                patch.object(service, '_release_lease'), \
                patch.object(service, '_record_failure') as record, \
                patch.object(service_module, 'WealthsimpleReader', return_value=reader):
            with self.assertRaisesRegex(ws.PortfolioError, 'reauth_required'):
                service.sync()

        reader.refresh_session.assert_called_once()
        record.assert_called_once_with('lease', 'portfolio', 'reauth_required')

    def test_fixed_queries_live_with_the_shared_client(self):
        query_dir = ROOT / 'src/wealthsimple_connector/core/queries'
        self.assertEqual({path.name for path in query_dir.glob('*.graphql')},
                         {'accounts.graphql', 'positions.graphql',
                          'balances.graphql', 'activities.graphql'})


if __name__ == '__main__':
    unittest.main()
