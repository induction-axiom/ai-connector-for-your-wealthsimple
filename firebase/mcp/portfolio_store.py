"""Read-only access to the current saved portfolio snapshot."""
from datetime import datetime, timezone
import json
from wealthsimple_connector.core.connection import connection_state
from wealthsimple_connector.core.portfolio import SnapshotError, project_portfolio, freshness


class PortfolioReader:
    def __init__(self, project, database, stale_seconds):
        from google.cloud import firestore
        self.db = firestore.Client(project=project, database=database)
        self.stale_seconds = stale_seconds

    def _documents(self):
        from google.cloud import firestore

        @firestore.transactional
        def read(tx):
            connector = self.db.collection("connectors").document("wealthsimple")
            state = connector.get(transaction=tx, timeout=10).to_dict() or {}
            status = state.get("portfolio", {})
            snapshot_id = status.get("current_snapshot_id")
            if not snapshot_id:
                return None, status, state
            snapshot = self.db.collection("portfolio_snapshots").document(snapshot_id)
            return snapshot.get(transaction=tx, timeout=10).to_dict(), status, state

        try:
            return read(self.db.transaction(read_only=True, max_attempts=2))
        except Exception:
            raise SnapshotError("portfolio_unavailable") from None

    @staticmethod
    def _snapshot(current):
        # Written by our own sync function; project_portfolio still allowlists every field.
        try:
            return json.loads(current["payload_json"])
        except (KeyError, TypeError, ValueError):
            raise SnapshotError("snapshot_shape_invalid") from None

    def load(self):
        try:
            current, status, state = self._documents()
            if not current:
                raise SnapshotError("no_snapshot")
            return self._snapshot(current), status, state
        except SnapshotError:
            raise
        except Exception:
            raise SnapshotError("portfolio_unavailable") from None

    def portfolio(self, **pagination):
        snapshot, status, _state = self.load()
        return project_portfolio(snapshot, status, stale_seconds=self.stale_seconds, **pagination)

    def status(self, now=None):
        try:
            current, status, state = self._documents()
            connection = connection_state(state, now or datetime.now(timezone.utc))
            reconnect = connection == "reconnect_required"
            if not current:
                if connection == "unknown":
                    # No saved session and no snapshot: a fresh deployment awaiting first connect.
                    connection = "not_connected"
                return {"snapshot_available": False,
                        "upstream_connection_checked": False,
                        "error": "no_snapshot", "reconnect_required": reconnect,
                        "connection_state": connection,
                        "account_count": status.get("account_count"),
                        "position_count": status.get("position_count")}
            snapshot = self._snapshot(current)
            return {"snapshot_available": True,
                    "upstream_connection_checked": False, "read_mode": "saved_snapshot",
                    **freshness(snapshot, status, self.stale_seconds),
                    "last_sync_error": status.get("error_code"),
                    "reconnect_required": reconnect, "connection_state": connection,
                    "account_count": status.get("account_count"),
                    "position_count": status.get("position_count")}
        except SnapshotError as error:
            return {"snapshot_available": False,
                    "upstream_connection_checked": False, "error": str(error),
                    "connection_state": "unknown", "account_count": None,
                    "position_count": None}
