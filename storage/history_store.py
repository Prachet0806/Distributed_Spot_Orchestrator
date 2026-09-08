# storage/history_store.py — migration history (insert-once, enrich-once).
"""Dynamo shape (when wired): table `spot_arbitrage_migration_history`,
PK `migration_id`, GSI `job_id`. Estimated vs actual stay distinct."""
from __future__ import annotations

from copy import deepcopy


class HistoryStore:
    def __init__(self, table_name: str = "spot_arbitrage_migration_history", dynamodb_resource=None):
        self.table_name = table_name
        self.dynamodb = dynamodb_resource
        self._records: dict[str, dict] = {}

    def insert(self, record: dict) -> dict:
        mid = record.get("migration_id")
        if not mid:
            raise ValueError("history record requires migration_id")
        if mid in self._records:
            raise KeyError(f"history {mid} already inserted")
        doc = deepcopy(record)
        doc["_enriched"] = False
        self._records[mid] = doc
        return deepcopy(doc)

    def enrich(self, migration_id: str, patch: dict) -> dict:
        try:
            doc = self._records[migration_id]
        except KeyError:
            raise KeyError(f"history {migration_id} not found")
        if doc.get("_enriched"):
            raise RuntimeError(f"history {migration_id} already enriched")
        for k, v in patch.items():
            if k in doc and k.startswith("estimated_"):
                raise RuntimeError(f"estimated field {k} immutable; write actual_* instead")
            doc[k] = deepcopy(v)
        doc["_enriched"] = True
        return deepcopy(doc)

    def get(self, migration_id: str) -> dict:
        try:
            return deepcopy(self._records[migration_id])
        except KeyError:
            raise KeyError(f"history {migration_id} not found")

    def list_by_job(self, job_id: str) -> list[dict]:
        return [deepcopy(r) for r in self._records.values() if r.get("job_id") == job_id]
