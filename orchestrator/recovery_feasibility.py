from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from enum import Enum

from orchestrator.candidate_compatibility import CompatibilityAssessment
from orchestrator.candidate_readiness import ReadinessAssessment
from orchestrator.workload_estimator import WorkloadEstimate


class FeasibilityStatus(str, Enum):
    FEASIBLE = "FEASIBLE"
    INFEASIBLE = "INFEASIBLE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


@dataclass
class PlanStepDuration:
    name: str
    estimated_seconds: Optional[float]
    confidence: float
    parallel_group: Optional[str] = None


@dataclass
class RecoveryFeasibility:
    status: FeasibilityStatus
    deadline_seconds: Optional[float]
    critical_path_seconds: Optional[float]
    safety_margin_seconds: float
    remaining_budget_seconds: Optional[float]
    steps: list[PlanStepDuration]
    assumptions: list[str]
    confidence: float
    feasibility_confidence: float
    evaluated_at: datetime
    infeasible_reason: Optional[str] = None


class RecoveryFeasibilityEngine:
    def __init__(self, safety_margin_factor: float = 1.3, min_safety_margin_seconds: float = 30):
        self.safety_margin_factor = safety_margin_factor
        self.min_safety_margin_seconds = min_safety_margin_seconds

    def evaluate(
        self,
        workload_estimate: WorkloadEstimate,
        compatibility_assessment: CompatibilityAssessment,
        readiness_assessment: ReadinessAssessment,
        interruption_deadline_seconds: Optional[float],
        plan_steps: Optional[list[PlanStepDuration]] = None,
    ) -> RecoveryFeasibility:
        assumptions = []
        # Option B: callers should pass real step estimates (see
        # build_emergency_plan_steps). None preserves the old crash-fix
        # behavior (empty plan => deadline-vs-margin check only).
        if plan_steps is None:
            plan_steps = []
            assumptions.append("No plan step estimates provided; critical path assumed 0s")

        critical_path = self._compute_critical_path(plan_steps, assumptions)
        safety_margin = max(
            critical_path * (self.safety_margin_factor - 1.0),
            self.min_safety_margin_seconds,
        )
        total_required = critical_path + safety_margin

        if interruption_deadline_seconds is None:
            return RecoveryFeasibility(
                status=FeasibilityStatus.INSUFFICIENT_EVIDENCE,
                deadline_seconds=None,
                critical_path_seconds=critical_path,
                safety_margin_seconds=safety_margin,
                remaining_budget_seconds=None,
                steps=plan_steps,
                assumptions=assumptions,
                confidence=0.0,
                feasibility_confidence=0.0,
                evaluated_at=datetime.utcnow(),
                infeasible_reason="No interruption deadline provided",
            )

        remaining_budget = interruption_deadline_seconds
        feasible = total_required <= interruption_deadline_seconds

        has_unknown = any(s.estimated_seconds is None for s in plan_steps)
        low_confidence = any(s.confidence < 0.5 for s in plan_steps)

        if has_unknown:
            assumptions.append("One or more step durations unknown")
            if not feasible:
                return RecoveryFeasibility(
                    status=FeasibilityStatus.INSUFFICIENT_EVIDENCE,
                    deadline_seconds=interruption_deadline_seconds,
                    critical_path_seconds=critical_path,
                    safety_margin_seconds=safety_margin,
                    remaining_budget_seconds=remaining_budget,
                    steps=plan_steps,
                    assumptions=assumptions,
                    confidence=0.0,
                    feasibility_confidence=0.0,
                    evaluated_at=datetime.utcnow(),
                    infeasible_reason="Unknown step durations prevent feasibility determination",
                )

        if low_confidence and not feasible:
            return RecoveryFeasibility(
                status=FeasibilityStatus.INSUFFICIENT_EVIDENCE,
                deadline_seconds=interruption_deadline_seconds,
                critical_path_seconds=critical_path,
                safety_margin_seconds=safety_margin,
                remaining_budget_seconds=remaining_budget,
                steps=plan_steps,
                assumptions=assumptions,
                confidence=0.0,
                feasibility_confidence=0.0,
                evaluated_at=datetime.utcnow(),
                infeasible_reason="Low confidence estimates prevent feasibility determination",
            )

        workload_confidence = workload_estimate.prediction_confidence
        candidate_confidence = min(
            compatibility_assessment.dimensions[0].status.value == "COMPATIBLE",
            readiness_assessment.status.value == "READY",
        )
        feasibility_confidence = min(workload_confidence, 0.9)

        if feasible:
            return RecoveryFeasibility(
                status=FeasibilityStatus.FEASIBLE,
                deadline_seconds=interruption_deadline_seconds,
                critical_path_seconds=critical_path,
                safety_margin_seconds=safety_margin,
                remaining_budget_seconds=remaining_budget - total_required,
                steps=plan_steps,
                assumptions=assumptions,
                confidence=min(workload_confidence, candidate_confidence),
                feasibility_confidence=feasibility_confidence,
                evaluated_at=datetime.utcnow(),
            )

        return RecoveryFeasibility(
            status=FeasibilityStatus.INFEASIBLE,
            deadline_seconds=interruption_deadline_seconds,
            critical_path_seconds=critical_path,
            safety_margin_seconds=safety_margin,
            remaining_budget_seconds=remaining_budget - total_required,
            steps=plan_steps,
            assumptions=assumptions,
            confidence=min(workload_confidence, candidate_confidence),
            feasibility_confidence=feasibility_confidence,
            evaluated_at=datetime.utcnow(),
            infeasible_reason=f"Required {total_required:.0f}s exceeds deadline {interruption_deadline_seconds:.0f}s",
        )

    def _compute_critical_path(
        self, steps: list[PlanStepDuration], assumptions: list[str]
    ) -> float:
        if not steps:
            return 0.0

        groups: dict[str, list[PlanStepDuration]] = {}
        sequential: list[PlanStepDuration] = []

        for step in steps:
            if step.parallel_group:
                if step.parallel_group not in groups:
                    groups[step.parallel_group] = []
                groups[step.parallel_group].append(step)
            else:
                sequential.append(step)

        total = 0.0

        for step in sequential:
            if step.estimated_seconds is None:
                assumptions.append(f"Step {step.name} duration unknown")
                return float("inf")
            total += step.estimated_seconds

        for group_name, group_steps in groups.items():
            group_max = 0.0
            for step in group_steps:
                if step.estimated_seconds is None:
                    assumptions.append(f"Parallel step {step.name} in {group_name} unknown")
                    return float("inf")
                group_max = max(group_max, step.estimated_seconds)
            total += group_max

        return total


# Option B (emergency step modeling): durations mirror
# MigrationPlanner._build_steps defaults, but checkpoint/transfer are
# filled from the live workload estimate instead of None. Unknown
# checkpoint duration stays None so evaluate() yields
# INSUFFICIENT_EVIDENCE (never a false FEASIBLE).
DEFAULT_PROVISION_SECONDS = 120.0
DEFAULT_TRANSFER_SECONDS = 60.0
DEFAULT_RESTORE_SECONDS = 60.0
TRANSFER_MBPS = 50.0

# Feedback keys (MigrationHistory.get_feedback_for_estimator) → step names.
_P95_FEEDBACK_MAP = {
    "actual_checkpoint_duration": "checkpointing",
    "actual_provision_duration": "provisioning",
    "actual_transfer_duration": "transferring",
    "actual_restore_duration": "restoring",
}


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        raise ValueError("no values")
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    rank = (pct / 100.0) * (len(sorted_vals) - 1)
    lo = int(rank)
    frac = rank - lo
    return float(sorted_vals[lo] + frac * (sorted_vals[min(lo + 1, len(sorted_vals) - 1)] - sorted_vals[lo]))


def p95_step_estimates(feedback: list[dict], min_samples: int = 5,
                       pct: float = 95.0) -> dict[str, float]:
    """Measured P95 durations per step from estimator feedback rows.

    Phase C: replaces guessed defaults with history. Steps with fewer
    than min_samples numeric observations are absent (caller keeps
    defaults). Never raises on ragged rows.
    """
    buckets: dict[str, list[float]] = {}
    for row in feedback or []:
        if not isinstance(row, dict):
            continue
        for fb_key, step in _P95_FEEDBACK_MAP.items():
            try:
                val = row.get(fb_key)
                if val is None:
                    continue
                buckets.setdefault(step, []).append(float(val))
            except (TypeError, ValueError):
                continue
    out: dict[str, float] = {}
    for step, vals in buckets.items():
        if len(vals) < min_samples:
            continue
        try:
            out[step] = _percentile(sorted(vals), pct)
        except ValueError:
            continue
    return out


def build_emergency_plan_steps(
    workload_estimate: WorkloadEstimate,
    transfer_mbps: float = TRANSFER_MBPS,
    measured_overrides: Optional[dict] = None,
) -> list[PlanStepDuration]:
    """Build feasibility step estimates from a workload estimate.

    Phase C: measured_overrides maps step name → P95 seconds (see
    p95_step_estimates). Measured values replace guessed defaults for
    checkpointing/provisioning/transferring/restoring; the provider
    deadline itself is never altered here.
    """
    try:
        conf = float(workload_estimate.prediction_confidence or 0.5)
    except (TypeError, ValueError):
        conf = 0.5
    conf = max(0.0, min(conf, 0.9))

    measured = dict(measured_overrides or {})

    ckpt_dur = measured.get("checkpointing")
    ckpt_measured = ckpt_dur is not None
    if ckpt_dur is None:
        ckpt_dur = getattr(workload_estimate, "checkpoint_duration_estimate_seconds", None)
    if ckpt_dur is None:
        checkpoint = PlanStepDuration("checkpointing", None, 0.4)
    else:
        checkpoint = PlanStepDuration("checkpointing", float(ckpt_dur),
                                      conf if not ckpt_measured else min(0.9, conf + 0.1))

    size = getattr(workload_estimate, "checkpoint_size_estimate_bytes", None)
    if "transferring" in measured:
        transfer = PlanStepDuration("transferring", float(measured["transferring"]), 0.8)
    elif size is None:
        transfer = PlanStepDuration("transferring", DEFAULT_TRANSFER_SECONDS, 0.5)
    else:
        try:
            secs = float(size) / (transfer_mbps * 1024 * 1024) + 5.0
        except (TypeError, ValueError):
            secs = DEFAULT_TRANSFER_SECONDS
        transfer = PlanStepDuration(
            "transferring", max(10.0, secs), conf if size else 0.5)

    prov_secs = float(measured.get("provisioning", DEFAULT_PROVISION_SECONDS))
    rest_secs = float(measured.get("restoring", DEFAULT_RESTORE_SECONDS))

    return [
        PlanStepDuration("prechecking", 10.0, 0.9),
        checkpoint,
        PlanStepDuration("persisting", 15.0, 0.6),
        PlanStepDuration("provisioning", prov_secs, 0.6 if "provisioning" not in measured else 0.8),
        transfer,
        PlanStepDuration("restoring", rest_secs, 0.6 if "restoring" not in measured else 0.8),
        PlanStepDuration("fencing", 30.0, 0.9),
        PlanStepDuration("validating", 15.0, 0.8),
        PlanStepDuration("activating", 10.0, 0.9),
        PlanStepDuration("finalizing", 10.0, 0.9),
    ]