from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from enum import Enum

from orchestrator.workload_estimator import WorkloadEstimate
from orchestrator.recovery_feasibility import RecoveryFeasibility


class CostComponent(str, Enum):
    CURRENT_COMPUTE = "current_compute"
    CANDIDATE_COMPUTE = "candidate_compute"
    CHECKPOINT_STORAGE = "checkpoint_storage"
    CHECKPOINT_TRANSFER = "checkpoint_transfer"
    PROVISIONING = "provisioning"
    RESTORE = "restore"
    VALIDATION = "validation"
    EXPECTED_RISK = "expected_risk"
    MIGRATION_OVERHEAD = "migration_overhead"


@dataclass
class CostBreakdown:
    current_cost_per_hour: float
    candidate_cost_per_hour: float
    checkpoint_storage_cost: float
    checkpoint_transfer_cost: float
    provisioning_cost: float
    restore_cost: float
    validation_cost: float
    expected_risk_cost: float
    migration_overhead_cost: float
    total_migration_cost: float
    expected_total_cost: float
    expected_savings: float
    completion_time_delta_seconds: float


@dataclass
class CostAnalysis:
    status: str
    cost_breakdown: CostBreakdown
    assumptions: list[str]
    confidence: float
    evaluated_at: datetime
    estimator_version: str
    risk_model_version: str


@dataclass
class RecoveryCostAnalysis:
    status: str
    recovery_cost: float
    alternative_cost: float
    assumptions: list[str]
    confidence: float
    evaluated_at: datetime


class CostRiskEvaluator:
    def __init__(
        self,
        storage_cost_per_gb_month: float = 0.023,
        transfer_cost_per_gb: float = 0.09,
        risk_model_version: str = "v2-basic-1",
    ):
        self.storage_cost_per_gb_month = storage_cost_per_gb_month
        self.transfer_cost_per_gb = transfer_cost_per_gb
        self.risk_model_version = risk_model_version

    def evaluate_stay(
        self,
        current_price_per_hour: float,
        workload_estimate: WorkloadEstimate,
        interruption_risk: float,
    ) -> CostAnalysis:
        remaining_hours = (
            workload_estimate.remaining_runtime_seconds / 3600
            if workload_estimate.remaining_runtime_seconds
            else 0
        )

        current_compute = current_price_per_hour * remaining_hours
        expected_risk = current_compute * interruption_risk

        breakdown = CostBreakdown(
            current_cost_per_hour=current_price_per_hour,
            candidate_cost_per_hour=0.0,
            checkpoint_storage_cost=0.0,
            checkpoint_transfer_cost=0.0,
            provisioning_cost=0.0,
            restore_cost=0.0,
            validation_cost=0.0,
            expected_risk_cost=expected_risk,
            migration_overhead_cost=0.0,
            total_migration_cost=0.0,
            expected_total_cost=current_compute + expected_risk,
            expected_savings=0.0,
            completion_time_delta_seconds=0.0,
        )

        return CostAnalysis(
            status="STAY_EVALUATED",
            cost_breakdown=breakdown,
            assumptions=["No migration performed", "Risk modeled as interruption probability"],
            confidence=workload_estimate.prediction_confidence,
            evaluated_at=datetime.utcnow(),
            estimator_version=workload_estimate.estimator_version,
            risk_model_version=self.risk_model_version,
        )

    def evaluate_candidate(
        self,
        current_price_per_hour: float,
        candidate_price_per_hour: float,
        workload_estimate: WorkloadEstimate,
        interruption_risk: float,
        checkpoint_size_bytes: Optional[int] = None,
        checkpoint_duration_seconds: Optional[float] = None,
        transfer_duration_seconds: Optional[float] = None,
        provision_duration_seconds: Optional[float] = None,
        restore_duration_seconds: Optional[float] = None,
        validation_duration_seconds: Optional[float] = None,
        provisioning_cost: float = 0.0,
        restore_cost: float = 0.0,
        validation_cost: float = 0.0,
    ) -> CostAnalysis:
        remaining_hours = (
            workload_estimate.remaining_runtime_seconds / 3600
            if workload_estimate.remaining_runtime_seconds
            else 0
        )

        current_compute = current_price_per_hour * remaining_hours
        candidate_compute = candidate_price_per_hour * remaining_hours

        checkpoint_size_gb = (checkpoint_size_bytes or 0) / (1024**3)
        storage_cost = checkpoint_size_gb * self.storage_cost_per_gb_month
        transfer_cost = checkpoint_size_gb * self.transfer_cost_per_gb

        migration_overhead_hours = 0.0
        if checkpoint_duration_seconds:
            migration_overhead_hours += checkpoint_duration_seconds / 3600
        if transfer_duration_seconds:
            migration_overhead_hours += transfer_duration_seconds / 3600
        if provision_duration_seconds:
            migration_overhead_hours += provision_duration_seconds / 3600
        if restore_duration_seconds:
            migration_overhead_hours += restore_duration_seconds / 3600
        if validation_duration_seconds:
            migration_overhead_hours += validation_duration_seconds / 3600

        migration_overhead_cost = migration_overhead_hours * current_price_per_hour
        expected_risk = candidate_compute * interruption_risk

        total_migration_cost = (
            storage_cost
            + transfer_cost
            + provisioning_cost
            + restore_cost
            + validation_cost
            + migration_overhead_cost
        )

        expected_total_cost = candidate_compute + total_migration_cost + expected_risk
        expected_savings = current_compute - expected_total_cost
        completion_time_delta = migration_overhead_hours * 3600

        breakdown = CostBreakdown(
            current_cost_per_hour=current_price_per_hour,
            candidate_cost_per_hour=candidate_price_per_hour,
            checkpoint_storage_cost=storage_cost,
            checkpoint_transfer_cost=transfer_cost,
            provisioning_cost=provisioning_cost,
            restore_cost=restore_cost,
            validation_cost=validation_cost,
            expected_risk_cost=expected_risk,
            migration_overhead_cost=migration_overhead_cost,
            total_migration_cost=total_migration_cost,
            expected_total_cost=expected_total_cost,
            expected_savings=expected_savings,
            completion_time_delta_seconds=completion_time_delta,
        )

        return CostAnalysis(
            status="CANDIDATE_EVALUATED",
            cost_breakdown=breakdown,
            assumptions=[
                "Linear cost projection",
                "Risk proportional to compute time",
                "Migration overhead additive",
            ],
            confidence=workload_estimate.prediction_confidence,
            evaluated_at=datetime.utcnow(),
            estimator_version=workload_estimate.estimator_version,
            risk_model_version=self.risk_model_version,
        )

    @staticmethod
    def short_job_band(remaining_runtime_seconds: Optional[float],
                       short_seconds: float = 900.0,
                       medium_seconds: float = 3600.0) -> str:
        """ADR-007 workload bands: SHORT / MEDIUM / LONG."""
        if not remaining_runtime_seconds:
            return "UNKNOWN"
        if remaining_runtime_seconds < short_seconds:
            return "SHORT"
        if remaining_runtime_seconds < medium_seconds:
            return "MEDIUM"
        return "LONG"

    @staticmethod
    def medium_band_net_benefit_ok(expected_savings: float,
                                   total_migration_cost: float,
                                   remaining_savings: float,
                                   multiplier: float = 2.0,
                                   savings_fraction: float = 0.20) -> tuple[bool, float]:
        """ADR-007 MEDIUM rule. Returns (ok, required_threshold)."""
        required = max(total_migration_cost * multiplier,
                       max(remaining_savings, 0.0) * savings_fraction)
        return expected_savings >= required, required

    def evaluate_recovery(
        self,
        recovery_feasibility: RecoveryFeasibility,
        current_price_per_hour: float,
        candidate_price_per_hour: float,
        workload_estimate: WorkloadEstimate,
    ) -> RecoveryCostAnalysis:
        recovery_cost = 0.0
        assumptions = []

        if recovery_feasibility.critical_path_seconds:
            recovery_hours = recovery_feasibility.critical_path_seconds / 3600
            recovery_cost = recovery_hours * candidate_price_per_hour
            assumptions.append(f"Recovery execution cost: {recovery_hours:.2f}h at ${candidate_price_per_hour}/h")

        if recovery_feasibility.steps:
            for step in recovery_feasibility.steps:
                if step.name == "checkpoint" and step.estimated_seconds:
                    assumptions.append(f"Checkpoint overhead: {step.estimated_seconds:.0f}s")
                if step.name == "provision" and step.estimated_seconds:
                    assumptions.append(f"Provisioning overhead: {step.estimated_seconds:.0f}s")

        alternative_cost = float("inf")
        if workload_estimate.remaining_runtime_seconds:
            alt_hours = workload_estimate.remaining_runtime_seconds / 3600
            alternative_cost = alt_hours * current_price_per_hour
            assumptions.append(f"Alternative (stay) cost: {alt_hours:.2f}h at ${current_price_per_hour}/h")

        return RecoveryCostAnalysis(
            status="RECOVERY_EVALUATED" if recovery_feasibility.status.value == "FEASIBLE" else "RECOVERY_INFEASIBLE",
            recovery_cost=recovery_cost,
            alternative_cost=alternative_cost,
            assumptions=assumptions,
            confidence=recovery_feasibility.feasibility_confidence,
            evaluated_at=datetime.utcnow(),
        )