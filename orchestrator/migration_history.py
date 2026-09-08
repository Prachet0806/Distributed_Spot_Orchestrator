from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Any
import uuid
import logging

logger = logging.getLogger(__name__)


class MigrationOutcome(str, Enum):
    SUCCESS = "SUCCESS"
    ABORTED = "ABORTED"
    SUPERSEDED = "SUPERSEDED"
    FAILED = "FAILED"


@dataclass
class MigrationRecord:
    migration_id: str
    job_id: str
    execution_epoch: int
    regime: str
    outcome: MigrationOutcome
    source_candidate_id: str
    target_candidate_id: str
    started_at: datetime
    completed_at: Optional[datetime] = None
    total_duration_seconds: Optional[float] = None
    policy_decision: dict = field(default_factory=dict)
    cost_analysis: dict = field(default_factory=dict)
    feasibility: dict = field(default_factory=dict)
    steps: list[dict] = field(default_factory=list)
    failure_reason: Optional[str] = None
    cleanup_status: str = "PENDING"
    estimator_version: str = ""
    risk_model_version: str = ""
    policy_config_version: str = ""
    planner_version: str = "v2-1"
    coordinator_version: str = "v2-1"


class MigrationHistory:
    def __init__(self, storage: Any = None):
        self.storage = storage
        self._records: list[MigrationRecord] = []

    def record_start(self, plan: Any, policy_decision: Any, cost_analysis: Any, feasibility: Any) -> str:
        record = MigrationRecord(
            migration_id=plan.migration_id,
            job_id=plan.job_id,
            execution_epoch=plan.execution_epoch,
            regime=plan.regime.value,
            outcome=MigrationOutcome.FAILED,
            source_candidate_id=plan.source_candidate_id,
            target_candidate_id=plan.target_candidate_id,
            started_at=datetime.utcnow(),
            policy_decision={
                "decision": policy_decision.decision.value,
                "regime": policy_decision.regime.value,
                "reason": policy_decision.reason,
                "confidence": policy_decision.confidence,
            },
            cost_analysis={
                "expected_total_cost": cost_analysis.cost_breakdown.expected_total_cost if cost_analysis else None,
                "expected_savings": cost_analysis.cost_breakdown.expected_savings if cost_analysis else None,
                "completion_time_delta": cost_analysis.cost_breakdown.completion_time_delta_seconds if cost_analysis else None,
            } if cost_analysis else {},
            feasibility={
                "status": feasibility.status.value if feasibility else None,
                "critical_path_seconds": feasibility.critical_path_seconds if feasibility else None,
                "confidence": feasibility.feasibility_confidence if feasibility else None,
            } if feasibility else {},
            estimator_version=plan.steps[0].__dict__.get("estimator_version", "") if plan.steps else "",
            risk_model_version="",
            policy_config_version="",
        )
        self._records.append(record)
        if self.storage:
            self.storage.save(record)
        return record.migration_id

    def record_step(self, migration_id: str, step_name: str, status: str, result: Any = None, error: str = None, duration: float = 0):
        record = self._get(migration_id)
        if record:
            record.steps.append({
                "step": step_name,
                "status": status,
                "result": str(result) if result else None,
                "error": error,
                "duration_seconds": duration,
                "timestamp": datetime.utcnow().isoformat(),
            })
            if self.storage:
                self.storage.save(record)

    def record_completion(self, migration_id: str, outcome: MigrationOutcome, failure_reason: str = None):
        record = self._get(migration_id)
        if record:
            record.outcome = outcome
            record.completed_at = datetime.utcnow()
            record.total_duration_seconds = (record.completed_at - record.started_at).total_seconds()
            record.failure_reason = failure_reason
            if self.storage:
                self.storage.save(record)

    def record_cleanup(self, migration_id: str, status: str):
        record = self._get(migration_id)
        if record:
            record.cleanup_status = status
            if self.storage:
                self.storage.save(record)

    def _get(self, migration_id: str) -> Optional[MigrationRecord]:
        for r in self._records:
            if r.migration_id == migration_id:
                return r
        return None

    def get_job_history(self, job_id: str) -> list[MigrationRecord]:
        return [r for r in self._records if r.job_id == job_id]

    def get_recent(self, limit: int = 100) -> list[MigrationRecord]:
        return sorted(self._records, key=lambda r: r.started_at, reverse=True)[:limit]

    def get_feedback_for_estimator(self, job_id: str) -> list[dict]:
        records = self.get_job_history(job_id)
        return [
            {
                "migration_id": r.migration_id,
                "actual_checkpoint_size": next((s for s in r.steps if s["step"] == "checkpointing"), {}).get("result", {}).get("size_bytes"),
                "actual_checkpoint_duration": next((s for s in r.steps if s["step"] == "checkpointing"), {}).get("duration_seconds"),
                "actual_provision_duration": next((s for s in r.steps if s["step"] == "provisioning"), {}).get("duration_seconds"),
                "actual_transfer_duration": next((s for s in r.steps if s["step"] == "transferring"), {}).get("duration_seconds"),
                "actual_restore_duration": next((s for s in r.steps if s["step"] == "restoring"), {}).get("duration_seconds"),
                "outcome": r.outcome.value,
                "regime": r.regime,
            }
            for r in records if r.completed_at
        ]

    def get_feedback_for_risk_model(self) -> list[dict]:
        records = [r for r in self._records if r.completed_at]
        return [
            {
                "regime": r.regime,
                "interruption_occurred": r.outcome == MigrationOutcome.FAILED and r.failure_reason and "interrupt" in r.failure_reason.lower(),
                "provisioning_succeeded": any(s["step"] == "provisioning" and s["status"] == "SUCCEEDED" for s in r.steps),
                "duration_seconds": r.total_duration_seconds,
                "cost_actual": None,
            }
            for r in records
        ]