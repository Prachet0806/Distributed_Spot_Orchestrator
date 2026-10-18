# storage/history_store.py — migration history (insert-once, enrich-once).
"""Dynamo shape (when wired): table `spot_arbitrage_migration_history`,
PK `migration_id`, GSI `job_id`. Estimated vs actual stay distinct."""
from __future__ import annotations

from copy import deepcopy
from storage.backend import Documents, encode, decode


class HistoryStore:
    def __init__(self, table_name: str = "spot_arbitrage_migration_history", dynamodb_resource=None):
        self.table_name = table_name
        self.dynamodb = dynamodb_resource
        self._docs = Documents("migration_id", table_name, dynamodb_resource)
        self._records = self._docs.local

    def insert(self, record: dict) -> dict:
        mid = record.get("migration_id")
        if not mid:
            raise ValueError("history record requires migration_id")
        if mid in self._records:
            raise KeyError(f"history {mid} already inserted")
        doc = deepcopy(record)
        doc["_enriched"] = False
        doc["version"] = 0
        return self._docs.put(doc, create=True)

    def enrich(self, migration_id: str, patch: dict) -> dict:
        try:
            doc = self.get(migration_id)
        except KeyError:
            raise KeyError(f"history {migration_id} not found")
        if doc.get("_enriched"):
            raise RuntimeError(f"history {migration_id} already enriched")
        for k, v in patch.items():
            if k in doc and k.startswith("estimated_"):
                raise RuntimeError(f"estimated field {k} immutable; write actual_* instead")
            doc[k] = deepcopy(v)
        doc["_enriched"] = True
        version = doc.get("version", 0)
        doc["version"] = version + 1
        return self._docs.put(doc, expected_version=version)

    def get(self, migration_id: str) -> dict:
        return self._docs.get(migration_id)

    def list_by_job(self, job_id: str) -> list[dict]:
        return [r for r in self._docs.all() if r.get("job_id") == job_id]

    def save(self, record) -> dict:
        """Upsert adapter for MigrationHistory (which calls storage.save).

        Accepts a MigrationRecord dataclass or a plain dict. Unlike
        insert/enrich (insert-once, enrich-once), save overwrites so the
        repeated record_start → record_step → record_completion →
        record_cleanup sequence persists every transition. Local write
        always applies; DynamoDB put is best-effort when wired.
        """
        from dataclasses import asdict, is_dataclass
        from datetime import datetime
        from enum import Enum

        if is_dataclass(record):
            doc = asdict(record)
        elif isinstance(record, dict):
            doc = deepcopy(record)
        else:
            mid = getattr(record, "migration_id", None)
            d = dict(getattr(record, "__dict__", {}))
            if mid is not None:
                d.setdefault("migration_id", mid)
            doc = deepcopy(d)

        def _norm(v):
            if isinstance(v, datetime):
                return v.isoformat()
            if isinstance(v, Enum):
                return v.value
            if isinstance(v, dict):
                return {k: _norm(x) for k, x in v.items()}
            if isinstance(v, (list, tuple)):
                return [_norm(x) for x in v]
            return v

        doc = {k: _norm(v) for k, v in doc.items()}
        mid = doc.get("migration_id")
        if not mid:
            raise ValueError("history record requires migration_id")
        with self._docs.lock:
            try:
                current = self.get(mid)
            except KeyError:
                current = None
            version = current.get("version", 0) if current else None
            doc["version"] = version + 1 if version is not None else 0
            return self._docs.put(doc, create=current is None, expected_version=version)
