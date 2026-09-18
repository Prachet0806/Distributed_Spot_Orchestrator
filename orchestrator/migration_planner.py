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
    absolute_deadline: Optional[datetime] = None
    estimated_critical_path_seconds: Optional[float] = None
    safety_margin_seconds: float = 30.0
    input_snapshot_versions: dict = field(default_factory=dict)
    policy_versions: dict = field(default_factory=dict)
    model_versions: dict = field(default_factory=dict)
    steps: list[PlanStepDuration] = field(default_factory=list)
    fencing_authorized: bool = True
    cleanup_rules: dict = field(default_factory=dict)
    authorization_reference: str = ""
    plan_version: str = "v2-1"
    plan_id: str = ""
    plan_hash: str = ""
    supersedes_plan_id: Optional[str] = None
    candidate_assessment_version: Optional[str] = None
    checkpoint_id: Optional[str] = None
    planner_version: str = "planner-v3"

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        return (now or datetime.utcnow()) > self.expires_at

    def is_emergency(self) -> bool:
        return self.regime == MigrationRegime.EMERGENCY


class MigrationPlanner:
    """Builds immutable, hash-pinned migration plans.

    TTLs per ADR-011: 600s arbitrage/proactive; emergency plans expire at
    the remaining interruption deadline. Fencing is authorized on every
    regime (ownership must move exactly once per migration).
    """

    def __init__(
        self,
        default_plan_ttl_hours: float = 1.0,  # legacy; prefer TTL constants below
        emergency_plan_ttl_minutes: float = 10.0,  # legacy fallback
        safety_margin_seconds: float = 30.0,
        arbitrage_ttl_seconds: float = 600.0,
        proactive_ttl_seconds: float = 600.0,
        planner_version: str = "planner-v3",
    ):
        self.default_plan_ttl_hours = default_plan_ttl_hours
        self.emergency_plan_ttl_minutes = emergency_plan_ttl_minutes
        self.safety_margin_seconds = safety_margin_seconds
        self.arbitrage_ttl_seconds = arbitrage_ttl_seconds
        self.proactive_ttl_seconds = proactive_ttl_seconds
        self.planner_version = planner_version
        # Plan-creation dedup index (Track C5): migration_id → latest plan.
        # Duplicate creation requests for the same logical migration (retry,
        # redelivered event) return the issued plan instead of minting a
        # second one. Durable (cross-restart) dedup rides the Plan Store
        # wiring (Track B1); this index covers in-process duplicates.
        self._issued: dict[str, MigrationPlan] = {}

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
        absolute_deadline: Optional[datetime] = None,
        checkpoint_id: Optional[str] = None,
        candidate_assessment_version: Optional[str] = None,
        supersedes_plan_id: Optional[str] = None,
        config_versions: Optional[dict] = None,
        migration_id: Optional[str] = None,
    ) -> MigrationPlan:
        hit = self._issued.get(migration_id or "")
        if hit is not None and (hit.supersedes_plan_id or "") == (supersedes_plan_id or ""):
            return hit  # duplicate creation request → idempotent return
        migration_id = migration_id or uuid.uuid4().hex
        plan_id = f"plan-{uuid.uuid4().hex[:16]}"
        created_at = datetime.utcnow()

        if regime == MigrationRegime.EMERGENCY:
            expires_at = absolute_deadline or (
                created_at + timedelta(minutes=self.emergency_plan_ttl_minutes)
            )
        elif regime == MigrationRegime.PROACTIVE:
            expires_at = created_at + timedelta(seconds=self.proactive_ttl_seconds)
        else:
            expires_at = created_at + timedelta(seconds=self.arbitrage_ttl_seconds)

        steps = self._build_steps(regime, recovery_feasibility)
        deadline = self._compute_deadline(regime, recovery_feasibility, workload_estimate)
        critical_path = self._estimate_critical_path(steps)

        input_versions = {
            "estimator_version": workload_estimate.estimator_version,
            "estimator_model_version": workload_estimate.model_version,
            "observation_snapshot": workload_estimate.observation_snapshot_version,
            "policy_version": getattr(policy_decision, "policy_version", "v3"),
            "planner_version": self.planner_version,
        }
        if stay_analysis:
            input_versions["risk_model_version"] = stay_analysis.risk_model_version
        if recovery_feasibility:
            input_versions["feasibility_confidence"] = str(recovery_feasibility.feasibility_confidence)
        if config_versions:
            input_versions["config_versions"] = dict(config_versions)

        cleanup_rules = self._build_cleanup_rules(regime)

        plan = MigrationPlan(
            migration_id=migration_id,
            plan_id=plan_id,
            job_id=job_id,
            execution_epoch=execution_epoch,
            regime=regime,
            source_pool_id=source_pool_id,
            target_pool_id=target_pool_id,
            policy_decision=policy_decision,
            created_at=created_at,
            expires_at=expires_at,
            deadline_seconds=deadline,
            absolute_deadline=absolute_deadline,
            estimated_critical_path_seconds=critical_path,
            safety_margin_seconds=self.safety_margin_seconds,
            input_snapshot_versions=input_versions,
            policy_versions={"policy": getattr(policy_decision, "policy_version", "v3")},
            steps=steps,
            fencing_authorized=True,
            cleanup_rules=cleanup_rules,
            authorization_reference=getattr(policy_decision, "decision_id", ""),
            supersedes_plan_id=supersedes_plan_id,
            candidate_assessment_version=candidate_assessment_version,
            checkpoint_id=checkpoint_id,
            planner_version=self.planner_version,
        )
        plan.plan_hash = self.compute_hash(plan)
        self._issued[plan.migration_id] = plan
        return plan

    def create_successor_plan(
        self,
        old_plan: MigrationPlan,
        reason: str,
        workload_estimate: WorkloadEstimate,
        policy_decision: Optional[PolicyDecision] = None,
        **overrides,
    ) -> MigrationPlan:
        """Replan path (Protocols §25; S15/S16): new immutable plan for the
        same migration attempt, linked via `supersedes_plan_id`.

        The old plan is NOT mutated; callers mark it SUPERSEDED through the
        Coordinator/Registry lifecycle. A fresh `workload_estimate` is
        required — replanning on stale evidence is prohibited. A new
        `migration_id` is required only when the execution attempt itself
        changes — replanning the same objective pre-fence keeps the attempt
        (and its operation IDs).
        """
        params = {
            "job_id": old_plan.job_id,
            "execution_epoch": old_plan.execution_epoch,
            "regime": old_plan.regime,
            "policy_decision": policy_decision or old_plan.policy_decision,
            "source_pool_id": old_plan.source_pool_id,
            "target_pool_id": old_plan.target_pool_id,
            "workload_estimate": workload_estimate,
            "absolute_deadline": old_plan.absolute_deadline,
            "checkpoint_id": old_plan.checkpoint_id,
            "candidate_assessment_version": old_plan.candidate_assessment_version,
            "supersedes_plan_id": old_plan.plan_id,
            "migration_id": old_plan.migration_id,
        }
        params.update(overrides)
        successor = self.create_plan(**params)
        successor.authorization_reference = (
            f"{successor.authorization_reference}|replan:{reason}"
        )
        return successor

    @staticmethod
    def compute_hash(plan: MigrationPlan) -> str:
        """SHA-256 over canonical immutable content (no volatile timestamps)."""
        import hashlib
        import json

        canonical = {
            "plan_id": plan.plan_id,
            "job_id": plan.job_id,
            "migration_id": plan.migration_id,
            "regime": plan.regime.value,
            "source_pool_id": plan.source_pool_id,
            "target_pool_id": plan.target_pool_id,
            "checkpoint_id": plan.checkpoint_id,
            "candidate_assessment_version": plan.candidate_assessment_version,
            "supersedes_plan_id": plan.supersedes_plan_id,
            "input_snapshot_versions": plan.input_snapshot_versions,
            "policy_versions": plan.policy_versions,
            "model_versions": plan.model_versions,
            "authorization_reference": plan.authorization_reference,
            "planner_version": plan.planner_version,
            "steps": [
                {"name": s.name, "estimated_seconds": s.estimated_seconds,
                 "parallel_group": s.parallel_group}
                for s in plan.steps
            ],
        }
        return hashlib.sha256(
            json.dumps(canonical, sort_keys=True, default=str).encode()
        ).hexdigest()

    def to_v2_dag(
        self,
        plan: MigrationPlan,
        injection_points: Optional[dict] = None,
    ) -> list:
        """Expand the plan into models_v2 PlanSteps with DAG dependencies.

        Serial regimes chain every step; EMERGENCY runs CHECKPOINT→PERSIST
        and PROVISION concurrently (join ALL_SUCCEEDED at TRANSFER).

        Steps carry §10.8 execution directives (retryable codes, priority).
        `injection_points` maps step-type name → test-only fault-injection
        hooks; production callers leave it empty (Coordinator rejects
        non-empty lists outside the test harness).
        """
        from orchestrator.models_v2 import (
            PlanStep, PlanStepType, STEP_ROLLBACK, STEP_CRITICALITY,
        )
        from orchestrator.step_retry import DEFAULT_RETRYABLE, DEFAULT_PRIORITY

        injection_points = injection_points or {}

        order = ["PRECHECK", "CHECKPOINT", "PERSIST", "PROVISION", "TRANSFER",
                 "RESTORE", "FENCE", "VALIDATE", "ACTIVATE", "FINALIZE"]
        op_map = {
            "PRECHECK": "Precheck", "CHECKPOINT": "CreateCheckpoint",
            "PERSIST": "PersistCheckpoint", "PROVISION": "Provision",
            "TRANSFER": "TransferCheckpoint", "RESTORE": "RestoreCheckpoint",
            "FENCE": "FenceSource", "VALIDATE": "ValidateMigration",
            "ACTIVATE": "ActivateTarget", "FINALIZE": "FinalizeMigration",
        }
        est = {s.name.lower(): s.estimated_seconds for s in plan.steps}
        timeouts = {"FENCE": 60.0}
        dag = []
        prev: list[str] = []
        ids_by_type: dict[str, str] = {}
        for name in order:
            stype = PlanStepType[name]
            if plan.regime == MigrationRegime.EMERGENCY:
                if name == "PROVISION":
                    deps = [ids_by_type["PRECHECK"]]
                elif name == "TRANSFER":
                    deps = [ids_by_type["PERSIST"], ids_by_type["PROVISION"]]
                else:
                    deps = list(prev)
            else:
                deps = list(prev)
            step = PlanStep(
                step_id=f"{plan.migration_id[:8]}-{name.lower()}",
                type=stype,
                depends_on=deps,
                operation_type=op_map[name],
                timeout_seconds=timeouts.get(name, 300.0),
                deadline_behavior="COMPLETE_FENCING" if name == "FENCE" else "FAIL_ON_EXCEED",
                max_attempts=3,
                retryable_failure_codes=sorted(DEFAULT_RETRYABLE.get(name, frozenset())),
                priority=DEFAULT_PRIORITY.get(name, 100),
                criticality=STEP_CRITICALITY[name],
                rollback_class=STEP_ROLLBACK[name],
                failure_injection_points=list(injection_points.get(name, [])),
            )
            seconds = est.get(name.lower())
            if seconds is not None:
                step.resource_requirements = {"estimated_seconds": seconds}
            dag.append(step)
            ids_by_type[name] = step.step_id
            prev = [step.step_id]
        return dag

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