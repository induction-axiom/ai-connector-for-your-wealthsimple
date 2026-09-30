"""Small transactional record store; credentials are addressed by SHA-256 hashes."""
from copy import deepcopy
from datetime import datetime, timezone
from threading import RLock


class MemoryStore:
    """Synthetic tests only; production always uses Firestore."""
    def __init__(self):
        self.data = {}
        self.lock = RLock()

    def get(self, key):
        with self.lock:
            return deepcopy(self.data.get(key))

    def transact(self, keys, change):
        with self.lock:
            result, writes = change({k: deepcopy(self.data.get(k)) for k in keys})
            self.data.update(deepcopy(writes))
            return result

    def list(self, prefix):
        with self.lock:
            return {k: deepcopy(v) for k, v in self.data.items() if k.startswith(prefix)}


class FirestoreStore:
    def __init__(self, project, database):
        from google.cloud import firestore
        self.db = firestore.Client(project=project, database=database)

    def ref(self, key):
        # This adapter cannot name a different database or collection.
        return self.db.collection("oauth_records").document(key)

    def get(self, key):
        return self.ref(key).get(timeout=10).to_dict()

    def list(self, prefix):
        from google.cloud.firestore_v1.base_query import FieldFilter
        query = (self.db.collection("oauth_records")
                 .where(filter=FieldFilter("__name__", ">=", self.ref(prefix)))
                 .where(filter=FieldFilter("__name__", "<", self.ref(prefix + "\uf8ff"))))
        return {doc.id: doc.to_dict() for doc in query.stream(timeout=10)}

    def transact(self, keys, change):
        from google.cloud import firestore

        @firestore.transactional
        def run(tx):
            current = {k: self.ref(k).get(transaction=tx, timeout=10).to_dict() for k in keys}
            result, writes = change(current)
            for key, value in writes.items():
                value = dict(value)
                if value.get("purge_at"):
                    value["purge_after"] = datetime.fromtimestamp(value["purge_at"], timezone.utc)
                tx.set(self.ref(key), value)
            return result

        return run(self.db.transaction(max_attempts=3))
