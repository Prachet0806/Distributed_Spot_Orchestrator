# orchestrator/models_v2.py — V2 domain contracts (Protocols #1, #3, #7–#11).
"""Additive V2 domain layer. Does not replace legacy planner/policy types yet;
new code should import from here. Legacy types remain frozen (ADR-024)."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class RecoveryDecisionKind(str, Enum):
    RETRY_RESTORE = "RETRY_RESTORE"
    FORWARD_RECOVERY = "FORWARD_RECOVERY"
    COLD_RESTART = "COLD_RESTART"
    REPLAN = "REPLAN"
    FAILED = "FAILED"


class ValidationResult(str, Enum):
    VALID = "VALID"
    INVALID = "INVALID"
    INCONCLUSIVE = "INCONCLUSIVE"


class ValidationLevel(str, Enum):
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L4 = "L4"


class PlanStepState(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    SKIPPED = "SKIPPED"


class PlanStepType(str, Enum):
    PRECHECK = "PRECHECK"
    CHECKPOINT = "CHECKPOINT"
    PERSIST = "PERSIST"
    PROVISION = "PROVISION"
    TRANSFER = "TRANSFER"
    RESTORE = "RESTORE"
    FENCE = "FENCE"
    VALIDATE = "VALIDATE"
    ACTIVATE = "ACTIVATE"
    FINALIZE = "FINALIZE"
    CLEANUP = "CLEANUP"


class RollbackClass(str, Enum):
    FULL_ROLLBACK = "FULL_ROLLBACK"
    PARTIAL_CLEANUP = "PARTIAL_CLEANUP"
    FORWARD_ONLY = "FORWARD_ONLY"
    COMPENSATING_ACTION = "COMPENSATING_ACTION"
    NONE = "NONE"


class Criticality(str, Enum):
    SAFETY_CRITICAL = "SAFETY_CRITICAL"
    CORRECTNESS_CRITICAL = "CORRECTNESS_CRITICAL"
    EXECUTION_CRITICAL = "EXECUTION_CRITICAL"
    BEST_EFFORT = "BEST_EFFORT"


# Normative per-step rollback/criticality (Protocols #4 §6.5, #8 §10.3).
STEP_ROLLBACK: dict[str, RollbackClass] = {
    "PRECHECK": RollbackClass.NONE,
    "CHECKPOINT": RollbackClass.FULL_ROLLBACK,
    "PERSIST": RollbackClass.FULL_ROLLBACK,
    "PROVISION": RollbackClass.FULL_ROLLBACK,
    "TRANSFER": RollbackClass.PARTIAL_CLEANUP,
    "RESTORE": RollbackClass.FULL_ROLLBACK,
    "FENCE": RollbackClass.FORWARD_ONLY,
    "VALIDATE": RollbackClass.FORWARD_ONLY,
    "ACTIVATE": RollbackClass.FORWARD_ONLY,
    "FINALIZE": RollbackClass.COMPENSATING_ACTION,
    "CLEANUP": RollbackClass.NONE,
}

STEP_CRITICALITY: dict[str, Criticality] = {
    "PRECHECK": Criticality.BEST_EFFORT,
    "CHECKPOINT": Criticality.CORRECTNESS_CRITICAL,
    "PERSIST": Criticality.EXECUTION_CRITICAL,
    "RESTORE": Criticality.CORRECTNESS_CRITICAL,
    "VALIDATE": Criticality.CORRECTNESS_CRITICAL,
    "PROVISION": Criticality.EXECUTION_CRITICAL,
    "TRANSFER": Criticality.EXECUTION_CRITICAL,
    "FENCE": Criticality.SAFETY_CRITICAL,
    "ACTIVATE": Criticality.EXECUTION_CRITICAL,
    "FINALIZE": Criticality.BEST_EFFORT,
    "CLEANUP": Criticality.BEST_EFFORT,
}


@dataclass
class PlanStep:
    step_id: str
    type: PlanStepType
    depends_on: list[str] = field(default_factory=list)
    operation_type: str = ""
    operation_id: Optional[str] = None  # assigned at issue by Coordinator (ULID)
    timeout_seconds: float = 60.0
    deadline_behavior: str = "FAIL_ON_EXCEED"  # FENCE: COMPLETE_FENCING
    max_attempts: int = 3
    backoff_base_seconds: float = 2.0
    backoff_max_seconds: float = 30.0
    retryable_failure_codes: list[str] = field(default_factory=list)
    criticality: Criticality = Criticality.EXECUTION_CRITICAL
    priority: int = 100
    join_policy: str = "ALL_SUCCEEDED"  # V2 only value
    rollback_class: RollbackClass = RollbackClass.NONE
    resource_requirements: dict = field(default_factory=dict)
    state: PlanStepState = PlanStepState.PENDING
    # Test-only fault-injection hooks (Protocols §10.8 rule 6). Production
    # plans MUST carry an empty list; the Coordinator rejects non-empty
    # lists unless the test harness flag is set.
    failure_injection_points: list[str] = field(default_factory=list)

    def __post_init__(self):
        if isinstance(self.type, str):
            self.type = PlanStepType(self.type)
        if isinstance(self.state, str):
            self.state = PlanStepState(self.state)
        if isinstance(self.rollback_class, str):
            self.rollback_class = RollbackClass(self.rollback_class)
        if isinstance(self.criticality, str):
            self.criticality = Criticality(self.criticality)
        if self.join_policy != "ALL_SUCCEEDED":
            raise ValueError("V2 supports only join_policy=ALL_SUCCEEDED")


@dataclass
class MigrationPlanV2:
    plan_id: str
    job_id: str
    migration_id: str
    regime: str  # ARBITRAGE | PROACTIVE | EMERGENCY
    source_pool_id: str
    target_pool_id: str
    checkpoint_id: Optional[str] = None
    candidate_assessment_version: Optional[str] = None
    created_at: str = ""
    expires_at: str = ""
    absolute_deadline: Optional[str] = None
    input_snapshot_versions: dict = field(default_factory=dict)
    policy_versions: dict = field(default_factory=dict)
    model_versions: dict = field(default_factory=dict)
    authorization_reference: str = ""
    planner_version: str = "v2"
    supersedes_plan_id: Optional[str] = None
    steps: list[PlanStep] = field(default_factory=list)
    plan_hash: str = ""

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        if not self.expires_at:
            return False
        ts = now or datetime.utcnow()
        return ts > datetime.fromisoformat(self.expires_at)


def canonical_plan_dict(plan: MigrationPlanV2) -> dict:
    """Immutable content only — excludes volatile timestamps/runtime state."""
    return {
        "plan_id": plan.plan_id,
        "job_id": plan.job_id,
        "migration_id": plan.migration_id,
        "regime": str(plan.regime),
        "source_pool_id": plan.source_pool_id,
        "target_pool_id": plan.target_pool_id,
        "checkpoint_id": plan.checkpoint_id,
        "candidate_assessment_version": plan.candidate_assessment_version,
        "input_snapshot_versions": plan.input_snapshot_versions,
        "policy_versions": plan.policy_versions,
        "model_versions": plan.model_versions,
        "authorization_reference": plan.authorization_reference,
        "planner_version": plan.planner_version,
        "supersedes_plan_id": plan.supersedes_plan_id,
        "steps": [
            {
                "step_id": s.step_id,
                "type": str(s.type),
                "depends_on": sorted(s.depends_on),
                "operation_type": s.operation_type,
                "timeout_seconds": s.timeout_seconds,
                "deadline_behavior": s.deadline_behavior,
                "max_attempts": s.max_attempts,
                "criticality": str(s.criticality),
                "priority": s.priority,
                "join_policy": s.join_policy,
                "rollback_class": str(s.rollback_class),
                "resource_requirements": s.resource_requirements,
                "failure_injection_points": sorted(s.failure_injection_points or []),
            }
            for s in plan.steps
        ],
    }


def compute_plan_hash(plan: MigrationPlanV2) -> str:
    canonical = json.dumps(canonical_plan_dict(plan), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class RecoveryDecision:
    decision_id: str
    job_id: str
    migration_id: str
    execution_epoch: int
    decision: RecoveryDecisionKind
    reason_code: str = ""
    failure_code: str = ""
    replan_required: bool = False
    checkpoint_id: Optional[str] = None
    lineage_id: Optional[str] = None
    critical_path_duration_seconds: Optional[float] = None
    safety_margin_seconds: float = 30.0
    confidence_level: str = "LOW"
    confidence_basis: dict = field(default_factory=dict)
    expires_at: str = ""
    policy_version: str = ""

    def __post_init__(self):
        if isinstance(self.decision, str):
            self.decision = RecoveryDecisionKind(self.decision)


@dataclass
class CheckpointRef:
    checkpoint_id: str
    lineage_id: str
    sequence: int
    execution_epoch: int
    durability: str = "LOCAL"  # LOCAL→PERSISTING→DURABLE→VALIDATED
    integrity_verified: bool = False
    lineage_valid: bool = False
    artifact_location: str = ""
    checksum: str = ""
    checksum_algorithm: str = "sha256"
    size_bytes: int = 0


@dataclass
class ValidationReport:
    migration_id: str
    job_id: str
    execution_epoch: int
    result: ValidationResult
    level_reached: ValidationLevel = ValidationLevel.L1
    epoch_match: bool = False
    lineage_valid: bool = False
    measurements: dict = field(default_factory=dict)
    contract_version: str = ""

    def __post_init__(self):
        if isinstance(self.result, str):
            self.result = ValidationResult(self.result)


@dataclass
class ReconciliationFinding:
    finding_id: str
    job_id: str
    migration_id: str
    execution_epoch: int
    mismatch_type: str
    observed_state: dict = field(default_factory=dict)
    authoritative_state: dict = field(default_factory=dict)
    severity: str = "MEDIUM"
    status: str = "OPEN"  # OPEN→ACKNOWLEDGED→REMEDIATING→RESOLVED|ESCALATED
    pre_authorized: bool = False
    detected_at: str = ""
    trigger: str = "EVENT_DRIVEN"


def new_ulid_like() -> str:
    """Placeholder until a ULID dependency lands; Coordinator is still the
    sole minter (Protocols #1). Format: UTC-ms + rand, lexicographically sortable."""
    import time
    import uuid

    ms = int(time.time() * 1000)
    return f"{ms:013d}{uuid.uuid4().hex[:13]}"


def to_dict(obj: Any) -> Any:
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, list):
        return [to_dict(x) for x in obj]
    if hasattr(obj, "__dataclass_fields__"):
        return {k: to_dict(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: to_dict(v) for k, v in obj.items()}
    return obj
