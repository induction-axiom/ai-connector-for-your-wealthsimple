"""One readable lifecycle for cloud session ownership, sync, and snapshots."""

from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
import time
from uuid import uuid4

from firebase_admin import firestore
from google.api_core.exceptions import FailedPrecondition, NotFound
from google.cloud import secretmanager

from wealthsimple_connector.core.client import (
    AccessTokenExpired,
    MfaRequired,
    PortfolioError,
    Session,
    WealthsimpleReader,
)
from wealthsimple_connector.core.activity import ACTIVITY_SCHEMA, normalize_activity
from wealthsimple_connector.core.connection import connection_state
from wealthsimple_connector.core.portfolio import reference


LEASE_SECONDS = 480
# Stop an activity sync after this long and let the caller continue it, well inside the
# functions' 300-second limit, so a long first backfill never ends in a timeout.
ACTIVITY_BUDGET_SECONDS = 180
# Each pass re-reads this much before the previous one ended, so pending rows that settle,
# late postings and removed rows are corrected.
ACTIVITY_OVERLAP = timedelta(days=30)
BACKFILL_START = "1900-01-01T00:00:00Z"
# A pass that finished this recently answers the next activity read without another sync,
# so consecutive questions reuse it. The dashboard's Sync can force a new pass.
ACTIVITY_FRESH_SECONDS = 600
MAX_SNAPSHOT_BYTES = 800_000
# Firestore allows 500 writes per transaction; one more is the connector state.
LEASED_WRITES_PER_COMMIT = 400
RECONNECT_ERRORS = {
    "reauth_required",
    "session_rotation_needs_recovery",
    "refresh_failed_reauth_may_be_required",
    "signed_out",
}


def _utc(value):
    """Parse an ISO time we or Wealthsimple wrote; None if it is not one."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


class PortfolioService:
    """Own a complete sync so credentials and leases do not leak into the reader."""

    def __init__(self, *, secret_id, database, currency, stale_seconds):
        self.secret_id = secret_id
        self.database = database
        self.currency = currency
        self.stale_seconds = stale_seconds

    @staticmethod
    def _now():
        return datetime.now(timezone.utc)

    @staticmethod
    def _project_id():
        value = os.environ.get("GCLOUD_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT")
        if not value:
            raise PortfolioError("cloud_project_id_missing")
        return value

    def _db(self):
        return firestore.client(database_id=self.database)

    @staticmethod
    def _connector_ref(db):
        return db.collection("connectors").document("wealthsimple")

    def _require_lease(self, state, lease_id):
        sync = state.get("sync", {})
        until = sync.get("lease_until")
        if (sync.get("lease_id") != lease_id or not isinstance(until, datetime)
                or until <= self._now()):
            raise PortfolioError("sync_lease_lost")

    def _acquire_lease(self, cooldown_seconds=0, allow_reconnect=False):
        db = self._db()
        lease_id = str(uuid4())

        @firestore.transactional
        def acquire(tx):
            ref = self._connector_ref(db)
            state = ref.get(transaction=tx).to_dict() or {}
            sync = state.get("sync", {})
            session = state.get("session", {})
            portfolio = state.get("portfolio", {})
            until = sync.get("lease_until")
            if isinstance(until, datetime) and until > self._now():
                raise PortfolioError("sync_already_running")
            if session.get("signed_out") and not allow_reconnect:
                raise PortfolioError("signed_out")
            if session.get("rotation_pending") and not allow_reconnect:
                raise PortfolioError("session_rotation_needs_recovery")

            now = self._now()
            last_requested = portfolio.get("last_refresh_requested_at")
            if (cooldown_seconds and isinstance(last_requested, datetime)
                    and now - last_requested < timedelta(seconds=cooldown_seconds)):
                raise PortfolioError("refresh_cooldown")
            values = {
                "sync": {
                    "lease_id": lease_id,
                    "lease_until": now + timedelta(seconds=LEASE_SECONDS),
                    "last_started_at": now,
                }
            }
            if cooldown_seconds:
                values["portfolio"] = {
                    **portfolio,
                    "last_refresh_requested_at": now,
                }
            tx.set(ref, values, merge=True)
            return session.get("secret_version")

        return lease_id, acquire(db.transaction())

    def _update_session_state(self, lease_id, values):
        db = self._db()

        @firestore.transactional
        def update(tx):
            ref = self._connector_ref(db)
            state = ref.get(transaction=tx).to_dict() or {}
            self._require_lease(state, lease_id)
            tx.set(ref, {"session": {**state.get("session", {}), **values}}, merge=True)

        update(db.transaction())

    def _record_failure(self, lease_id, section, code):
        """Mark a section (portfolio, activities, keep_alive) as failed, unless another
        worker owns the lease now. True if a session rotation was left unfinished."""
        db = self._db()

        @firestore.transactional
        def record(tx):
            ref = self._connector_ref(db)
            state = ref.get(transaction=tx).to_dict() or {}
            if state.get("sync", {}).get("lease_id") != lease_id:
                return False
            values = {section: {**state.get(section, {}), "status": "error",
                                "error_code": code, "last_finished_at": self._now()}}
            # A refresh token may have been spent without its replacement being saved.
            unfinished = state.get("session", {}).get("rotation_pending") is True
            if code in RECONNECT_ERRORS or unfinished:
                values["session"] = {**state.get("session", {}), "reconnect_required": True}
            tx.set(ref, values, merge=True)
            return unfinished

        try:
            return record(db.transaction())
        except Exception:
            logging.error("sync_status_write_failed")
            return False

    def _commit_under_lease(self, db, lease_id, writes):
        """Apply (ref, value, merge) writes, where value None deletes, in transactions that
        each confirm this worker still holds the lease. Later writes land in later commits."""
        for start in range(0, len(writes), LEASED_WRITES_PER_COMMIT):
            part = writes[start:start + LEASED_WRITES_PER_COMMIT]

            @firestore.transactional
            def commit(tx):
                state = self._connector_ref(db).get(transaction=tx).to_dict() or {}
                self._require_lease(state, lease_id)
                for ref, value, merge in part:
                    if value is None:
                        tx.delete(ref)
                    else:
                        tx.set(ref, value, merge=merge)

            commit(db.transaction())

    def _release_lease(self, lease_id):
        db = self._db()

        @firestore.transactional
        def release(tx):
            ref = self._connector_ref(db)
            sync = (ref.get(transaction=tx).to_dict() or {}).get("sync", {})
            if sync.get("lease_id") == lease_id:
                tx.set(ref, {"sync": {**sync, "lease_id": None,
                                      "lease_until": datetime.fromtimestamp(0, timezone.utc)}},
                       merge=True)

        try:
            release(db.transaction())
        except Exception:
            logging.error("sync_lease_release_failed")

    @contextmanager
    def _lease(self, section=None, **acquire):
        """Hold the one sync lease for an operation; record failures under `section`."""
        lease_id = None
        try:
            lease_id, version_name = self._acquire_lease(**acquire)
            yield lease_id, version_name
        except Exception as error:
            code = str(error) if isinstance(error, PortfolioError) else "cloud_dependency_failed"
            if (lease_id and section and self._record_failure(lease_id, section, code)
                    and code not in RECONNECT_ERRORS):
                # The section keeps the original code; the caller is told how to recover.
                logging.error("session_rotation_unfinished code=%s", code)
                raise PortfolioError("session_rotation_needs_recovery") from None
            if isinstance(error, PortfolioError):
                raise
            raise PortfolioError(code) from None
        finally:
            if lease_id:
                self._release_lease(lease_id)

    def _rotating(self, lease_id, reader):
        """Run reads; on the first expired access token, rotate the session once and retry."""
        rotated = False

        def read(call):
            nonlocal rotated
            try:
                return call()
            except AccessTokenExpired:
                if rotated:
                    raise PortfolioError("reauth_required") from None
                rotated = True
                self._update_session_state(lease_id, {"rotation_pending": True})
                self._save_session(lease_id, reader.refresh_session())
                try:
                    return call()
                except AccessTokenExpired:
                    raise PortfolioError("reauth_required") from None

        return read

    def _secret_parent(self):
        return f"projects/{self._project_id()}/secrets/{self.secret_id}"

    def _load_session(self, lease_id, version_name):
        parent = self._secret_parent()
        response = secretmanager.SecretManagerServiceClient().access_secret_version(
            request={"name": version_name or parent + "/versions/latest"}, timeout=15)
        # Pin the exact version this worker read, in our own project-id form.
        exact = parent + "/versions/" + response.name.rsplit("/", 1)[-1]
        self._update_session_state(lease_id, {"secret_version": exact})
        return Session.from_json(response.payload.data.decode("utf-8"))

    def _save_session(self, lease_id, session):
        # Check ownership again before publishing the rotated session version.
        state = self._connector_ref(self._db()).get().to_dict() or {}
        self._require_lease(state, lease_id)
        previous = (state.get("session") or {}).get("secret_version")
        parent = self._secret_parent()
        client = secretmanager.SecretManagerServiceClient()
        response = client.add_secret_version(
            request={"parent": parent,
                     "payload": {"data": session.to_json().encode("utf-8")}},
            timeout=15)
        # Failure here deliberately leaves rotation_pending set for recovery.
        self._update_session_state(lease_id, {
            "secret_version": parent + "/versions/" + response.name.rsplit("/", 1)[-1],
            "rotation_pending": False,
            "reconnect_required": False,
            "signed_out": False,
        })
        # The replaced session's refresh token is spent. Destroy it so versions don't pile up
        # (each enabled version is billed); if this fails, the next rotation leaves one extra.
        if previous:
            try:
                client.destroy_secret_version(request={"name": previous}, timeout=15)
            except Exception:
                pass

    def reconnect(self, username, password, otp=None):
        """Replace the cloud session; credentials are never persisted."""
        if (not isinstance(username, str) or not 3 <= len(username) <= 254
                or not isinstance(password, str) or not 1 <= len(password) <= 1024
                or (otp is not None and (not isinstance(otp, str)
                    or not 4 <= len(otp) <= 20))):
            raise PortfolioError("reconnect_input_invalid")

        # A failed sign-in is not a sync failure, so nothing is recorded (no section).
        with self._lease(allow_reconnect=True) as (lease_id, version_name):
            try:
                session = self._load_session(lease_id, version_name)
            except (NotFound, FailedPrecondition):
                if version_name:
                    raise
                session = Session()  # First connection, or after signing out: nothing usable.
            with closing(WealthsimpleReader(session)) as reader:
                if not session.client_id or not session.session_id or not session.wssdi:
                    reader.bootstrap_session()
                self._save_session(lease_id, reader.reconnect_session(username, password, otp))
        return {"result": "reconnect_succeeded"}

    def sign_out(self):
        """Forget the Wealthsimple session by destroying every stored copy. Saved data stays."""
        with self._lease(allow_reconnect=True) as (lease_id, _version):
            client = secretmanager.SecretManagerServiceClient()
            for version in client.list_secret_versions(
                    request={"parent": self._secret_parent(), "filter": "state:ENABLED"}, timeout=15):
                client.destroy_secret_version(request={"name": version.name}, timeout=15)
            self._update_session_state(lease_id, {"secret_version": None, "signed_out": True,
                                                  "reconnect_required": False,
                                                  "rotation_pending": False})
        return {"result": "signed_out"}

    def _save_snapshot(self, lease_id, snapshot):
        payload = json.dumps(snapshot, separators=(",", ":"), ensure_ascii=False)
        if len(payload.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
            raise PortfolioError("snapshot_too_large")

        db = self._db()
        snapshot_id = str(uuid4())
        document = {
            "payload_json": payload,
            "fetched_at": snapshot["fetched_at"],
            "provider": "wealthsimple",
        }

        @firestore.transactional
        def save(tx):
            ref = self._connector_ref(db)
            state = ref.get(transaction=tx).to_dict() or {}
            self._require_lease(state, lease_id)
            if state.get("session", {}).get("rotation_pending"):
                raise PortfolioError("session_rotation_needs_recovery")

            now = self._now()
            status = {
                "status": "ok",
                "error_code": None,
                "last_success_at": now,
                "last_finished_at": now,
                "last_fetched_at": snapshot["fetched_at"],
                "account_count": len(snapshot["accounts"]),
                "position_count": len(snapshot["positions"]),
                "current_snapshot_id": snapshot_id,
            }
            sync = state.get("sync", {})
            tx.create(db.collection("portfolio_snapshots").document(snapshot_id), document)
            tx.set(ref, {
                "portfolio": {**state.get("portfolio", {}), **status},
                "sync": {
                    **sync,
                    "lease_id": None,
                    "lease_until": datetime.fromtimestamp(0, timezone.utc),
                },
            }, merge=True)

        save(db.transaction())

    def keep_session_alive(self):
        """Trade the refresh token for a fresh one, so the sign-in doesn't lapse between uses."""
        # Its own section: a failed keep-alive says nothing about the saved portfolio.
        with self._lease("keep_alive") as (lease_id, version), \
                closing(WealthsimpleReader(self._load_session(lease_id, version))) as reader:
            self._update_session_state(lease_id, {"rotation_pending": True})
            self._save_session(lease_id, reader.refresh_session())
            db, now = self._db(), self._now()
            self._commit_under_lease(db, lease_id, [(self._connector_ref(db), {"keep_alive": {
                "status": "ok", "error_code": None, "last_success_at": now,
                "last_finished_at": now}}, True)])

    def sync(self, cooldown_seconds=0):
        """Read one complete portfolio snapshot and publish it."""
        with self._lease("portfolio", cooldown_seconds=cooldown_seconds) as (lease_id, version), \
                closing(WealthsimpleReader(self._load_session(lease_id, version))) as reader:
            snapshot = self._rotating(lease_id, reader)(lambda: reader.collect(self.currency))
            self._save_snapshot(lease_id, snapshot)
        return {
            "result": "refresh_succeeded",
            "fetched_at": snapshot["fetched_at"],
            "account_count": len(snapshot["accounts"]),
            "position_count": len(snapshot["positions"]),
        }

    def sync_activity(self, force=False):
        """Read one pass of activity: all history for new accounts, and for the others
        everything since shortly before the previous pass, so late or changed rows are fixed.
        Unless forced, a pass finished in the last ACTIVITY_FRESH_SECONDS is reused instead."""
        reused = None if force else self._fresh_activity()
        if reused:
            return reused
        with self._lease("activities") as (lease_id, version), \
                closing(WealthsimpleReader(self._load_session(lease_id, version))) as reader:
            read = self._rotating(lease_id, reader)
            accounts = read(reader.activity_accounts)
            db = self._db()
            state = self._activity_pass(
                (self._connector_ref(db).get().to_dict() or {}).get("activities", {}), accounts)
            started = time.monotonic()
            seen_cursors = {state["cursor"]} if state["cursor"] else set()
            while state["account_index"] < len(accounts):
                account = accounts[state["account_index"]]
                start = self._activity_start(state, account)
                nodes, next_cursor = read(lambda: reader.activity_page(
                    account["id"], start, state["window_end"], state["cursor"]))
                if next_cursor in seen_cursors:
                    raise PortfolioError("pagination_cursor_invalid")
                seen_cursors = seen_cursors | {next_cursor} if next_cursor else set()
                state = self._save_activity_page(db, lease_id, state, account, start,
                                                 nodes, next_cursor)
                if time.monotonic() - started > ACTIVITY_BUDGET_SECONDS:
                    break  # progress is saved; the next call continues from here
            if not accounts:
                state = {**state, "status": "complete", "pass_finished_at": self._now().isoformat(),
                         "last_fetched_at": self._now().isoformat()}
            state["last_success_at"] = self._now()
            self._commit_under_lease(db, lease_id,
                                     [(self._connector_ref(db), {"activities": state}, True)])
        return self._activity_summary(state)

    def _activity_pass(self, state, accounts):
        """Continue an unfinished pass over the same accounts, or start the next one."""
        fingerprint = hashlib.sha256(
            "\0".join(account["id"] for account in accounts).encode()).hexdigest()
        if state.get("schema") != ACTIVITY_SCHEMA:
            # First sync, or saved rows predate the current row shape: read all history again.
            state = {"schema": ACTIVITY_SCHEMA, "synced_accounts": [], "account_gaps": {},
                     "earliest_occurred_at": None, "window_start": BACKFILL_START}
        elif (state.get("account_set_hash") == fingerprint
              and state.get("status") not in {"complete", "partial"}):
            return state  # an earlier call stopped mid-pass, on time or on an error

        # Accounts already read up to window_end are read again from OVERLAP before it.
        # A pass cut short by an account change keeps its own, earlier start as well.
        starts = [_utc(state["window_end"]) - ACTIVITY_OVERLAP] if state.get("window_end") else []
        if state.get("status") not in {"complete", "partial"} and state.get("incremental_start"):
            starts.append(_utc(state["incremental_start"]))
        now = self._now().isoformat()
        current = {reference(account["id"]) for account in accounts}
        return {**state, "account_set_hash": fingerprint, "account_count": len(accounts),
                "account_gaps": {ref: start for ref, start in state["account_gaps"].items()
                                 if ref in current},
                "account_index": 0, "cursor": None, "account_skipped": 0,
                "pages_fetched": 0, "rows_seen": 0, "skipped_count": 0,
                "status": "running", "error_code": None,
                "incremental_start": min(starts).isoformat() if starts else None,
                "pass_started_at": now, "window_end": now}

    @staticmethod
    def _activity_start(state, account):
        ref = reference(account["id"])
        if ref not in state["synced_accounts"]:
            return BACKFILL_START  # new account, or first sync: all available history
        # A range that could not be read cleanly before is read again until it is.
        gap = state["account_gaps"].get(ref)
        if gap and _utc(gap) < _utc(state["incremental_start"]):
            return gap
        return state["incremental_start"]

    def _save_activity_page(self, db, lease_id, state, account, start, nodes, next_cursor):
        observed_at = self._now().isoformat()
        rows = [normalize_activity(node, observed_at, account["type"]) for node in nodes]
        valid = [row for row in rows if row is not None]
        skipped = len(nodes) - len(valid)
        earliest = min((row["occurred_ts"].isoformat() for row in valid), default=None)
        state = {**state, "status": "running", "error_code": None,
                 "pages_fetched": state["pages_fetched"] + 1,
                 "rows_seen": state["rows_seen"] + len(valid),
                 "skipped_count": state["skipped_count"] + skipped,
                 "account_skipped": state["account_skipped"] + skipped,
                 "earliest_occurred_at": min(filter(None, (
                     state.get("earliest_occurred_at"), earliest)), default=None),
                 "cursor": next_cursor,
                 "last_fetched_at": observed_at}
        activities = db.collection("activities")
        writes = [(activities.document(row["activity_ref"]), row, False) for row in valid]
        if not next_cursor:  # this account's window is fully read
            # A row we could not read may be one we saved before, so only a clean read
            # can show that Wealthsimple removed a row.
            if not state["account_skipped"]:
                kept = {row["activity_ref"] for row in valid}
                writes += [(ref, None, False) for ref in
                           self._unseen_activities(db, account, start, state) if ref.id not in kept]
            # Coverage stays incomplete until this whole window reads without a skipped row.
            ref = reference(account["id"])
            gaps = {key: value for key, value in state["account_gaps"].items() if key != ref}
            if state["account_skipped"]:
                gaps[ref] = start
            state = {**state, "account_index": state["account_index"] + 1, "account_skipped": 0,
                     "account_gaps": gaps,
                     "synced_accounts": sorted({*state["synced_accounts"], ref})}
            if state["account_index"] == state["account_count"]:
                state["status"] = "partial" if gaps else "complete"
                state["pass_finished_at"] = self._now().isoformat()
        self._commit_under_lease(db, lease_id, [
            *writes, (self._connector_ref(db), {"activities": state}, True)])
        return state

    @staticmethod
    def _unseen_activities(db, account, start, state):
        """Saved rows in this account's window that were not read again in this pass."""
        pass_started = _utc(state["pass_started_at"])
        # Strictly inside the window: Wealthsimple's own bounds may exclude either end.
        query = (db.collection("activities")
                 .where(filter=firestore.FieldFilter("account_ref", "==", reference(account["id"])))
                 .where(filter=firestore.FieldFilter("occurred_ts", ">", _utc(start)))
                 .where(filter=firestore.FieldFilter("occurred_ts", "<", _utc(state["window_end"])))
                 .order_by("occurred_ts", direction=firestore.Query.DESCENDING)
                 .order_by("activity_ref", direction=firestore.Query.DESCENDING))
        unseen = []
        for document in query.stream(timeout=30):
            observed = _utc((document.to_dict() or {}).get("observed_at"))
            if observed is not None and observed < pass_started:
                unseen.append(document.reference)
        return unseen

    def _fresh_activity(self):
        """The saved pass's summary if it can answer this read, else None to sync.

        Only a finished pass is reused: an unfinished backfill keeps going, and a connection
        that needs signing in again goes on to report that. Reuse never hides a gap: the
        summary still says whether coverage is complete.
        """
        state = self._connector_ref(self._db()).get().to_dict() or {}
        activities, now = state.get("activities") or {}, self._now()
        finished = _utc(activities.get("pass_finished_at"))
        if (activities.get("schema") != ACTIVITY_SCHEMA
                or activities.get("status") not in {"complete", "partial"}
                or finished is None or not 0 <= (now - finished).total_seconds() < ACTIVITY_FRESH_SECONDS
                or connection_state(state, now) != "connected"):
            return None
        return {**self._activity_summary(activities), "result": "refresh_reused",
                "pass_finished_at": finished.isoformat(),
                "fresh_until": (finished + timedelta(seconds=ACTIVITY_FRESH_SECONDS)).isoformat()}

    @staticmethod
    def _activity_summary(state):
        return {
            "result": {"complete": "refresh_succeeded",
                       "running": "refresh_continues"}.get(state.get("status"), "refresh_partial"),
            "fetched_at": state.get("last_fetched_at"),
            "account_count": state.get("account_count"),
            "rows_processed": state.get("rows_seen"),
            "coverage_complete": state.get("status") == "complete",
            "earliest_occurred_at": state.get("earliest_occurred_at"),
        }
