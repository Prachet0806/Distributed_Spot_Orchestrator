import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class TransferResult:
    migration_id: str
    bytes_transferred: int | None
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
                "No transfer storage configured: refusing to fake a transfer"
            )

    def upload(
        self, job_id: str, src: str | None = None, operation_id: str | None = None
    ) -> TransferResult:
        import time

        # I7 executor-side idempotency: redelivery of a completed
        # operation_id returns the recorded result instead of
        # re-uploading bytes (mirrors Provisioner replay).
        if operation_id and operation_id in self._operations:
            logger.info("Transfer replay %s -> recorded result", operation_id[:8])
            return self._operations[operation_id]
        self._require_storage()
        started = time.monotonic()
        key = (
            self.storage.upload(job_id)
            if src is None
            else self.storage.upload(job_id, src=src)
        )
        manifest_fn = getattr(self.storage, "manifest", None)
        manifest = manifest_fn(job_id) if callable(manifest_fn) else None
        if manifest is not None and manifest["artifact"]["key"] != key:
            raise RuntimeError("uploaded artifact does not match its manifest")
        result = TransferResult(
            migration_id=job_id,
            bytes_transferred=(
                int(manifest["artifact"]["size_bytes"]) if manifest else None
            ),
            duration_seconds=time.monotonic() - started,
            status="COMPLETED",
        )
        if operation_id:
            self._operations[operation_id] = result
        return result

    def download(
        self, job_id: str, dst: str | None = None, operation_id: str | None = None
    ) -> TransferResult:
        import time

        if operation_id and operation_id in self._operations:
            logger.info("Transfer replay %s -> recorded result", operation_id[:8])
            return self._operations[operation_id]
        self._require_storage()
        started = time.monotonic()
        if dst is None:
            manifest = self.storage.download(job_id)
        else:
            manifest = self.storage.download(job_id, dst=dst)
        if manifest is None:
            manifest_fn = getattr(self.storage, "manifest", None)
            manifest = manifest_fn(job_id) if callable(manifest_fn) else None
        result = TransferResult(
            migration_id=job_id,
            bytes_transferred=(
                int(manifest["artifact"]["size_bytes"]) if manifest else None
            ),
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
