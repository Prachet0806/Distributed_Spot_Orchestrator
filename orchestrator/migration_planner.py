from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional
from enum import Enum
import uuid

from orchestrator.policy_engine import PolicyDecision, MigrationRegime
from orchestrator.candidate_compatibility import CompatibilityAssessment
from orchestrator.candidate_readiness import ReadinessAssessment
from orchestrator.recovery_feasibility import RecoveryFeasibility, PlanStepDuration
from orchestrator.workload_estimator import WorkloadEstimate
from orchestrator.cost_risk_evaluator import CostAnalysis


class MigrationState(str, Enum):
    PLANNED = "PLANNED"
    PRECHECKING = "PRECHECKING"
    CHECKPOINTING = "CHECKPOINTING"
    PERSISTING = "PERSISTING"
    PROVISIONING = "PROVISIONING"
    TRANSFERRING = "TRANSFERRING"
    RESTORING = "RESTORING"
    FENCING = "FENCING"
    VALIDATING = "VALIDATING"
    ACTIVATING = "ACTIVATING"
    FINALIZING = "FINALIZING"
    SUCCESS = "SUCCESS"
    ABORTED = "ABORTED"
    SUPERSEDED = "SUPERSEDED"
    FAILED = "FAILED"
    ABORTING = "ABORTING"


@dataclass
class MigrationPlan:
    migration_id: str
    job_id: str
    execution_epoch: int
    regime: MigrationRegime
    source_pool_id: str
    target_pool_id: str
    policy_decision: PolicyDecision
    created_at: datetime
    expires_at: datetime
    deadline_seconds: Optional[float] = None
    estimated_critical_path_seconds: Optional[float] = None
    safety_margin_seconds: float = 30.0
    input_snapshot_versions: dict = field(default_factory=dict)
    steps: list[PlanStepDuration] = field(default_factory=list)
    fencing_authorized: bool = False
    cleanup_rules: dict = field(default_factory=dict)
    plan_version: str = "v2-1"

    def is_expired(self) -> bool:
        return datetime.utcnow() > self.expires_at

    def is_emergency(self) -> bool:
        return self.regime == MigrationRegime.EMERGENCY


class MigrationPlanner:
    def __init__(
        self,
        default_plan_ttl_hours: float = 1.0,
        emergency_plan_ttl_minutes: float = 10.0,
        safety_margin_seconds: float = 30.0,
    ):
        self.default_plan_ttl_hours = default_plan_ttl_hours
        self.emergency_plan_ttl_minutes = emergency_plan_ttl_minutes
        self.safety_margin_seconds = safety_margin_seconds

    def create_plan(
        self,
        job_id: str,
        execution_epoch: int,
        regime: MigrationRegime,
        policy_decision: PolicyDecision,
        source_pool_id: str,
        target_pool_id: str,
        workload_estimate: WorkloadEstimate,
        recovery_feasibility: Optional[RecoveryFeasibility] = None,
        stay_analysis: Optional[CostAnalysis] = None,
        candidate_analysis: Optional[CostAnalysis] = None,
    ) -> MigrationPlan:
        migration_id = str(uuid.uuid4())[:8]
        created_at = datetime.utcnow()

        if regime == MigrationRegime.EMERGENCY:
            expires_at = created_at + timedelta(minutes=self.emergency_plan_ttl_minutes)
        else:
            expires_at = created_at + timedelta(hours=self.default_plan_ttl_hours)

        steps = self._build_steps(regime, recovery_feasibility)
        deadline = self._compute_deadline(regime, recovery_feasibility, workload_estimate)
        critical_path = self._estimate_critical_path(steps)
        fencing_auth = regime in (MigrationRegime.PROACTIVE, MigrationRegime.EMERGENCY)

        input_versions = {
            "estimator_version": workload_estimate.estimator_version,
            "estimator_model_version": workload_estimate.model_version,
            "observation_snapshot": workload_estimate.observation_snapshot_version,
        }
        if stay_analysis:
            input_versions["risk_model_version"] = stay_analysis.risk_model_version
        if recovery_feasibility:
            input_versions["feasibility_confidence"] = str(recovery_feasibility.feasibility_confidence)

        cleanup_rules = self._build_cleanup_rules(regime)

        return MigrationPlan(
            migration_id=migration_id,
            job_id=job_id,
            execution_epoch=execution_epoch,
            regime=regime,
            source_pool_id=source_pool_id,
            target_pool_id=target_pool_id,
            policy_decision=policy_decision,
            created_at=created_at,
            expires_at=expires_at,
            deadline_seconds=deadline,
            estimated_critical_path_seconds=critical_path,
            safety_margin_seconds=self.safety_margin_seconds,
            input_snapshot_versions=input_versions,
            steps=steps,
            fencing_authorized=fencing_auth,
            cleanup_rules=cleanup_rules,
        )

    def _build_steps(
        self, regime: MigrationRegime, feasibility: Optional[RecoveryFeasibility]
    ) -> list[PlanStepDuration]:
        steps = [
            PlanStepDuration("prechecking", 10.0, 0.9),
            PlanStepDuration("checkpointing", None, 0.5),
            PlanStepDuration("persisting", None, 0.6),
        ]

        if regime == MigrationRegime.EMERGENCY and feasibility:
            checkpoint_est = next(
                (s.estimated_seconds for s in feasibility.steps if s.name == "checkpoint"), None
            )
            provision_est = next(
                (s.estimated_seconds for s in feasibility.steps if s.name == "provision"), None
            )
            transfer_est = next(
                (s.estimated_seconds for s in feasibility.steps if s.name == "transfer"), None
            )
            restore_est = next(
                (s.estimated_seconds for s in feasibility.steps if s.name == "restore"), None
            )
            validate_est = next(
                (s.estimated_seconds for s in feasibility.steps if s.name == "validate"), None
            )

            if checkpoint_est:
                steps[1] = PlanStepDuration("checkpointing", checkpoint_est, 0.7)
            if provision_est:
                steps.append(PlanStepDuration("provisioning", provision_est, 0.7, parallel_group="prep"))
            if transfer_est:
                steps.append(PlanStepDuration("transferring", transfer_est, 0.8))
            if restore_est:
                steps.append(PlanStepDuration("restoring", restore_est, 0.7))
            if validate_est:
                steps.append(PlanStepDuration("validating", validate_est, 0.8))

            steps.append(PlanStepDuration("fencing", 30.0, 0.9))
            steps.append(PlanStepDuration("activating", 10.0, 0.9))
            steps.append(PlanStepDuration("finalizing", 10.0, 0.9))

            return steps

        steps.extend([
            PlanStepDuration("provisioning", 120.0, 0.6),
            PlanStepDuration("transferring", 60.0, 0.7),
            PlanStepDuration("restoring", 60.0, 0.6),
            PlanStepDuration("fencing", 30.0, 0.9),
            PlanStepDuration("activating", 10.0, 0.9),
            PlanStepDuration("finalizing", 10.0, 0.9),
        ])

        return steps

    def _compute_deadline(
        self,
        regime: MigrationRegime,
        feasibility: Optional[RecoveryFeasibility],
        workload_estimate: WorkloadEstimate,
    ) -> Optional[float]:
        if regime == MigrationRegime.EMERGENCY and feasibility and feasibility.deadline_seconds:
            return feasibility.deadline_seconds
        return None

    def _estimate_critical_path(self, steps: list[PlanStepDuration]) -> Optional[float]:
        sequential = [s for s in steps if not s.parallel_group]
        parallel_groups: dict[str, list[PlanStepDuration]] = {}
        for s in steps:
            if s.parallel_group:
                parallel_groups.setdefault(s.parallel_group, []).append(s)

        total = sum(s.estimated_seconds for s in sequential if s.estimated_seconds)
        for group in parallel_groups.values():
            group_max = max(s.estimated_seconds for s in group if s.estimated_seconds)
            total += group_max

        return total if total > 0 else None

    def _build_cleanup_rules(self, regime: MigrationRegime) -> dict:
        base = {
            "on_abort_pre_fencing": ["terminate_target", "delete_partial_artifacts"],
            "on_failure_pre_fencing": ["terminate_target", "delete_partial_artifacts", "preserve_source"],
            "on_failure_post_fencing": ["retain_target_for_recovery", "forward_recovery_only"],
        }
        if regime == MigrationRegime.EMERGENCY:
            base["deadline_exceeded_during_fencing"] = "complete_fencing_then_forward_recovery"
        return base