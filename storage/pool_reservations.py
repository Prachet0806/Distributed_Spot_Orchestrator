"""CAS-protected migration reservations survive controller restarts."""
from storage.backend import Documents, conditional_failure


class PoolReservations:
    def __init__(self, table_name="spot_arbitrage_pool_reservations", dynamodb_resource=None):
        self._docs = Documents("pool_id", table_name, dynamodb_resource)

    def reserve(self, pool_id, migration_id, limit):
        if not isinstance(limit, int) or limit <= 0:
            return False
        with self._docs.lock:
            for _ in range(5):
                try:
                    doc = self._docs.get(pool_id)
                except KeyError:
                    doc = {"pool_id": pool_id, "version": -1, "migrations": []}
                if migration_id in doc["migrations"]:
                    return True
                if len(doc["migrations"]) >= limit:
                    return False
                version = doc["version"]
                doc.update(version=version + 1, migrations=[*doc["migrations"], migration_id])
                try:
                    self._docs.put(doc, create=version == -1, expected_version=version if version >= 0 else None)
                    return True
                except Exception as exc:
                    if not (conditional_failure(exc) or isinstance(exc, KeyError)):
                        raise
            return False

    def release(self, pool_id, migration_id):
        with self._docs.lock:
            doc = self._docs.get(pool_id)
            version = doc["version"]
            doc.update(version=version + 1, migrations=[m for m in doc["migrations"] if m != migration_id])
            self._docs.put(doc, expected_version=version)
