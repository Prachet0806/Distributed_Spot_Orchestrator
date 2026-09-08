from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional, Any, Callable
import logging

logger = logging.getLogger(__name__)


class CleanupType(str, Enum):
    MIGRATION_OWNED = "MIGRATION_OWNED"
    ORPHAN = "ORPHAN"
    POST_FENCING = "POST_FENCING"
    SAFETY_CRITICAL = "SAFETY_CRITICAL"


class CleanupStatus(str, Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    DEFERRED = "DEFERRED"


@dataclass
class CleanupTask:
    task_id: str
    cleanup_type: CleanupType
    resource_type: str
    resource_id: str
    parameters: dict
    priority: int
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    status: CleanupStatus = CleanupStatus.PENDING
    result: Optional[Any] = None
    error: Optional[str] = None


class CleanupExecutor:
    def __init__(
        self,
        provisioner: Any,
        storage_manager: Any,
        checkpoint_manager: Any,
        operation_handlers: dict[str, Callable] = None,
    ):
        self.provisioner = provisioner
        self.storage_manager = storage_manager
        self.checkpoint_manager = checkpoint_manager
        self.operation_handlers = operation_handlers or {}

    def cleanup_migration(
        self,
        plan: Any,
        operations: list,
        safety_critical_only: bool = False,
    ) -> list[str]:
        completed = []

        if not safety_critical_only:
            self._cleanup_partial_artifacts(plan.migration_id)
            completed.append("partial_artifacts")

        if plan.target_candidate_id:
            self._terminate_target_instance(plan.target_candidate_id)
            completed.append("target_instance")

        return completed

    def cleanup_orphan(self, resource_id: str, resource_type: str = "instance") -> bool:
        try:
            if resource_type == "instance":
                self.provisioner.terminate(resource_id)
            elif resource_type == "checkpoint":
                self.storage_manager.delete(resource_id)
            elif resource_type == "volume":
                self._cleanup_volume(resource_id)
            return True
        except Exception as e:
            logger.error(f"Orphan cleanup failed for {resource_type} {resource_id}: {e}")
            return False

    def _cleanup_partial_artifacts(self, migration_id: str):
        try:
            self.storage_manager.cleanup_migration_artifacts(migration_id)
        except Exception as e:
            logger.warning(f"Partial artifact cleanup failed for {migration_id}: {e}")

    def _terminate_target_instance(self, instance_id: str):
        try:
            self.provisioner.terminate(instance_id)
            logger.info(f"Terminated target instance {instance_id}")
        except Exception as e:
            logger.error(f"Failed to terminate target instance {instance_id}: {e}")
            raise

    def _cleanup_volume(self, volume_id: str):
        pass

    def execute_cleanup_plan(self, tasks: list) -> dict:
        results = {}
        for task in tasks:
            try:
                if task["type"] == "terminate_instance":
                    self.provisioner.terminate(task["resource_id"])
                elif task["type"] == "delete_artifact":
                    self.storage_manager.delete(task["resource_id"])
                elif task["type"] == "cleanup_checkpoint":
                    self.checkpoint_manager.cleanup(task["resource_id"])
                results[task["task_id"]] = "COMPLETED"
            except Exception as e:
                results[task["task_id"]] = f"FAILED: {e}"
        return results