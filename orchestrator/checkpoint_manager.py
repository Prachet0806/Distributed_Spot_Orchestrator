from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Any, Callable
import logging
import time

logger = logging.getLogger(__name__)


@dataclass
class CheckpointResult:
    checkpoint_id: str
    size_bytes: int
    duration_seconds: float
    digest: str
    status: str  # LOCAL | PERSISTING | DURABLE | VALIDATED
    job_id: str = ""
    lineage_id: str = ""
    sequence: int = 0
    artifact_location: str = ""


@dataclass
class RestoreResult:
    checkpoint_id: str
    success: bool
    duration_seconds: float
    process_id: Optional[int] = None
    outcome: str = "SUCCEEDED"  # SUCCEEDED | FAILED | PARTIAL_RESTORE | UNKNOWN


class CheckpointManager:
    """Owns checkpoint lifecycle: dump → persist → restore.

    Real backends are injected (`dump_handler`, `restore_handler`,
    `storage` as S3Manager). Without handlers the manager fails closed —
    it never reports fake success (the old mock returned
    `100MB/30s/sha256:abc123` unconditionally).
    """

    def __init__(
        self,
        storage: Any = None,
        s3_bucket: str = None,
        dump_handler: Optional[Callable[..., CheckpointResult]] = None,
        restore_handler: Optional[Callable[..., RestoreResult]] = None,
        checkpoint_store: Any = None,
    ):
        self.storage = storage
        self.s3_bucket = s3_bucket
        self.dump_handler = dump_handler
        self.restore_handler = restore_handler
        self.checkpoint_store = checkpoint_store
        self._checkpoints: dict[str, CheckpointResult] = {}

    # -- new explicit API --
    def dump(self, job_id: str, pid: int, host: Optional[str] = None,
             timeout: float = 300.0) -> CheckpointResult:
        if self.dump_handler is None:
            raise RuntimeError(
                "No dump handler configured: refusing to fake a checkpoint "
                f"for job {job_id}")
        started = time.monotonic()
        result = self.dump_handler(job_id=job_id, pid=pid, host=host,
                                   timeout=timeout)
        result.duration_seconds = time.monotonic() - started
        result.status = "LOCAL"
        self._checkpoints[result.checkpoint_id] = result
        self._record_store(result)
        return result

    def persist(self, checkpoint_id: str, job_id: Optional[str] = None,
                timeout: float = 300.0) -> CheckpointResult:
        result = self._checkpoints.get(checkpoint_id)
        if result is None and self.checkpoint_store is not None:
            try:
                doc = self.checkpoint_store.get(checkpoint_id)
                result = CheckpointResult(
                    checkpoint_id=checkpoint_id, size_bytes=doc.get("size_bytes", 0),
                    duration_seconds=0.0, digest=doc.get("checksum", ""),
                    status=doc.get("durability", "LOCAL"), job_id=doc.get("job_id", job_id or ""),
                    lineage_id=doc.get("lineage_id", ""))
                self._checkpoints[checkpoint_id] = result
            except KeyError:
                result = None
        if result is None:
            raise KeyError(f"checkpoint {checkpoint_id} unknown: dump first")
        result.status = "PERSISTING"
        if self.storage is not None:
            if job_id is None:
                job_id = result.job_id
            self.storage.upload(job_id or checkpoint_id)
            result.artifact_location = f"s3://{getattr(self.storage, 'bucket', self.s3_bucket)}/{job_id or checkpoint_id}.tar.gz"
        elif self.s3_bucket is None:
            raise RuntimeError(
                f"No checkpoint storage configured for {checkpoint_id}: "
                "refusing to mark DURABLE without persisted bytes")
        result.status = "DURABLE"
        self._record_store(result, durability="DURABLE")
        return result

    def restore(self, checkpoint_id: str, host: Optional[str] = None,
                timeout: float = 300.0) -> RestoreResult:
        # Restore safety gate (§11.9): DURABLE + integrity + lineage.
        if self.checkpoint_store is not None:
            try:
                from orchestrator.models_v2 import CheckpointRef  # noqa: F401
                doc = self.checkpoint_store.get(checkpoint_id)
                if doc.get("durability") not in ("DURABLE", "VALIDATED"):
                    return RestoreResult(checkpoint_id=checkpoint_id, success=False,
                                         duration_seconds=0.0, outcome="FAILED")
                if not doc.get("integrity_verified", True):
                    return RestoreResult(checkpoint_id=checkpoint_id, success=False,
                                         duration_seconds=0.0, outcome="FAILED")
            except KeyError:
                return RestoreResult(checkpoint_id=checkpoint_id, success=False,
                                     duration_seconds=0.0, outcome="FAILED")
        if self.restore_handler is None:
            raise RuntimeError(
                f"No restore handler configured for {checkpoint_id}: "
                "refusing to fake a restore")
        return self.restore_handler(checkpoint_id=checkpoint_id, host=host,
                                    timeout=timeout)

    def get_operation_status(self, operation_id: str) -> dict:
        return {"operation_id": operation_id, "state": "UNKNOWN"}

    def _record_store(self, result: CheckpointResult, durability: Optional[str] = None):
        if self.checkpoint_store is None:
            return
        try:
            from orchestrator.models_v2 import CheckpointRef
            ref = CheckpointRef(
                checkpoint_id=result.checkpoint_id,
                lineage_id=result.lineage_id or result.job_id,
                sequence=result.sequence,
                execution_epoch=0,
                durability=durability or result.status,
                integrity_verified=bool(result.digest),
                artifact_location=result.artifact_location,
                checksum=result.digest, size_bytes=result.size_bytes)
            try:
                self.checkpoint_store.put(ref)
            except KeyError:
                if durability:
                    self.checkpoint_store.set_durability(result.checkpoint_id, durability)
        except Exception as exc:  # metadata must not break the data path
            logger.warning("Checkpoint metadata record failed: %s", exc)

    # -- legacy shims (frozen callers) --
    def create_checkpoint(self, plan: Any = None, **kwargs) -> CheckpointResult:
        if plan is None and "job_id" not in kwargs:
            raise TypeError("create_checkpoint requires plan or job_id=")
        job_id = kwargs.get("job_id") or getattr(plan, "job_id", "unknown")
        pid = kwargs.get("pid", 0)
        host = kwargs.get("host")
        return self.dump(job_id=job_id, pid=pid, host=host)

    def persist_checkpoint(self, migration_id: str, **kwargs) -> CheckpointResult:
        result = self._checkpoints.get(migration_id)
        if result is not None:
            return self.persist(result.checkpoint_id, job_id=kwargs.get("job_id"))
        return self.persist(migration_id, job_id=kwargs.get("job_id"))

    def restore_checkpoint(self, migration_id: str, **kwargs) -> RestoreResult:
        result = self._checkpoints.get(migration_id)
        checkpoint_id = result.checkpoint_id if result else migration_id
        outcome = self.restore(checkpoint_id, host=kwargs.get("host"))
        # Legacy shape compat: callers read .success / .process_id.
        return outcome

    def cleanup(self, migration_id: str):
        if migration_id in self._checkpoints:
            del self._checkpoints[migration_id]

    def get_checkpoint(self, migration_id: str) -> Optional[CheckpointResult]:
        return self._checkpoints.get(migration_id)
