# storage/audit_store.py — append-only audit records.
"""Dynamo shape (when wired): table `spot_arbitrage_audit`, PK `audit_id`,
GSI (job_id, timestamp). No UPDATE/DELETE outside archival (ADR-015)."""
from __future__ import annotations

from copy import deepcopy
from storage.backend import Documents, conditional_failure


class AuditStore:
    def __init__(self, table_name: str = "spot_arbitrage_audit", dynamodb_resource=None):
        self.table_name = table_name
        self.dynamodb = dynamodb_resource
        self._docs = Documents("audit_id", table_name, dynamodb_resource)
        self._records = self._docs.local

    def append(self, record: dict) -> dict:
        aid = record.get("audit_id")
        if not aid:
            raise ValueError("audit record requires audit_id")
        if aid in self._records:
            raise KeyError(f"audit {aid} already recorded (append-only)")
        try:
            return self._docs.put(record, create=True)
        except Exception as exc:
            if conditional_failure(exc):
                raise KeyError(f"audit {aid} already recorded") from exc
            raise

    def list_by_job(self, job_id: str) -> list[dict]:
        return sorted(
            (deepcopy(r) for r in self._docs.all() if r.get("job_id") == job_id),
            key=lambda r: r.get("timestamp", ""),
        )
