# storage/audit_store.py — append-only audit records.
"""Dynamo shape (when wired): table `spot_arbitrage_audit`, PK `audit_id`,
GSI (job_id, timestamp). No UPDATE/DELETE outside archival (ADR-015)."""
from __future__ import annotations

from copy import deepcopy


class AuditStore:
    def __init__(self, table_name: str = "spot_arbitrage_audit", dynamodb_resource=None):
        self.table_name = table_name
        self.dynamodb = dynamodb_resource
        self._records: dict[str, dict] = {}

    def append(self, record: dict) -> dict:
        aid = record.get("audit_id")
        if not aid:
            raise ValueError("audit record requires audit_id")
        if aid in self._records:
            raise KeyError(f"audit {aid} already recorded (append-only)")
        self._records[aid] = deepcopy(record)
        if self.dynamodb is not None:
            self.dynamodb.Table(self.table_name).put_item(
                Item=deepcopy(record),
                ConditionExpression="attribute_not_exists(audit_id)")
        return deepcopy(record)

    def list_by_job(self, job_id: str) -> list[dict]:
        return sorted(
            (deepcopy(r) for r in self._records.values() if r.get("job_id") == job_id),
            key=lambda r: r.get("timestamp", ""),
        )
