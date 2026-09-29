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
        rows = [deepcopy(r) for r in self._records.values() if r.get("job_id") == job_id]
        if self.dynamodb is not None:
            # Cross-process read: scan the durable table for this job.
            # Best-effort; local rows win on migration_id conflict.
            try:
                table = self.dynamodb.Table(self.table_name)
                resp = table.scan(
                    FilterExpression="job_id = :j",
                    ExpressionAttributeValues={":j": job_id},
                )
                for item in resp.get("Items", []):
                    if item.get("migration_id") not in self._records:
                        rows.append(deepcopy(item))
            except Exception:
                pass
        return rows

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
        self._records[mid] = deepcopy(doc)
        if self.dynamodb is not None:
            try:
                self.dynamodb.Table(self.table_name).put_item(Item=deepcopy(doc))
            except Exception:
                pass
        return deepcopy(doc)
