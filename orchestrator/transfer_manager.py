from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Any
import logging

logger = logging.getLogger(__name__)


@dataclass
class TransferResult:
    migration_id: str
    bytes_transferred: int
    duration_seconds: float
    status: str


class TransferManager:
    """Moves checkpoint bytes job ↔ S3 with integrity (ADR-016).

    Without `storage` (S3Manager) every call fails closed — the old stub
    returned fixed `100MB/15s/COMPLETED` without moving any bytes.
    """

    def __init__(self, storage: Any = None):
        self.storage = storage
        self._operations: dict[str, TransferResult] = {}

    def _require_storage(self):
        if self.storage is None:
            raise RuntimeError(
                "No transfer storage configured: refusing to fake a transfer")

    def upload(self, job_id: str, src: Optional[str] = None,
               operation_id: Optional[str] = None) -> TransferResult:
        import time
        self._require_storage()
        started = time.monotonic()
        key = self.storage.upload(job_id) if src is None else self.storage.upload(job_id, src=src)
        result = TransferResult(
            migration_id=job_id,
            bytes_transferred=0,
            duration_seconds=time.monotonic() - started,
            status="COMPLETED",
        )
        if operation_id:
            self._operations[operation_id] = result
        return result

    def download(self, job_id: str, dst: Optional[str] = None,
                 operation_id: Optional[str] = None) -> TransferResult:
        import time
        self._require_storage()
        started = time.monotonic()
        if dst is None:
            self.storage.download(job_id)
        else:
            self.storage.download(job_id, dst=dst)
        result = TransferResult(
            migration_id=job_id,
            bytes_transferred=0,
            duration_seconds=time.monotonic() - started,
            status="COMPLETED",
        )
        if operation_id:
            self._operations[operation_id] = result
        return result

    def get_operation_status(self, operation_id: str) -> dict:
        result = self._operations.get(operation_id)
        if result is None:
            return {"operation_id": operation_id, "state": "UNKNOWN"}
        return {"operation_id": operation_id, "state": "SUCCEEDED"}

    # -- legacy shims --
    def transfer(self, migration_id: str) -> TransferResult:
        return self.upload(migration_id)
