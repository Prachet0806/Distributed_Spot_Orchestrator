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
        plan_steps: list[PlanStepDuration],
    ) -> RecoveryFeasibility:
        assumptions = []
        step_dict = {s.name: s for s in plan_steps}

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