from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional, List
import time
import uuid

from orchestrator.cost_risk_evaluator import CostAnalysis, RecoveryCostAnalysis
from orchestrator.recovery_feasibility import RecoveryFeasibility
from orchestrator.candidate_compatibility import CompatibilityAssessment
from orchestrator.candidate_readiness import ReadinessAssessment
from orchestrator.workload_estimator import WorkloadEstimate


class Decision(str, Enum):
    STAY = "STAY"
    MIGRATE = "MIGRATE"
    DEFER = "DEFER"
    RECOVER = "RECOVER"
    ABANDON = "ABANDON"


class MigrationRegime(str, Enum):
    ARBITRAGE = "ARBITRAGE"
    PROACTIVE = "PROACTIVE"
    EMERGENCY = "EMERGENCY"


@dataclass
class ArbitragePolicyConfig:
    min_savings_margin: float = 0.10
    cooldown_hours: float = 0.25  # deprecated alias; prefer cooldown_seconds (ADR-006: 900s)
    cooldown_seconds: float = 900.0
    min_favorable_observations: int = 3
    max_completion_overhead_ratio: float = 0.20
    short_runtime_seconds: float = 900.0
    medium_runtime_seconds: float = 3600.0
    medium_net_benefit_multiplier: float = 2.0
    medium_required_savings_fraction: float = 0.20
    workload_thresholds: dict[str, float] = None
    policy_version: str = "v3"

    def __post_init__(self):
        if self.workload_thresholds is None:
            self.workload_thresholds = {
                "short": 0.0,
                "medium": 0.25,
                "long": 0.12,
                "stateful": 0.40,
            }
        # Back-compat: explicit cooldown_hours wins if non-default was passed.
        if self.cooldown_hours != 0.25:
            self.cooldown_seconds = self.cooldown_hours * 3600.0

    @property
    def effective_cooldown_seconds(self) -> float:
        return self.cooldown_seconds


@dataclass
class ProactivePolicyConfig:
    risk_threshold: float = 0.7
    rebalance_threshold: float = 0.20
    cooldown_hours: float = 1.0


@dataclass
class RecoveryPolicyConfig:
    min_feasibility_confidence: float = 0.5
    allow_cold_restart: bool = False
    max_recovery_time_ratio: float = 1.0


@dataclass
class PolicyDecision:
    decision: Decision
    regime: MigrationRegime
    reason: str
    target_candidate_id: Optional[str]
    confidence: float
    metadata: dict
    decision_id: str = ""
    policy_version: str = "v3"
    expires_at: Optional[datetime] = None

    def __post_init__(self):
        if not self.decision_id:
            self.decision_id = f"dec-{uuid.uuid4().hex[:12]}"


class ArbitragePolicy:
    def __init__(self, config: ArbitragePolicyConfig = None):
        self.config = config or ArbitragePolicyConfig()

    def decide(
        self,
        stay_analysis: CostAnalysis,
        candidate_analyses: list[CostAnalysis],
        workload_estimate: WorkloadEstimate,
        compatibility_assessments: List[CompatibilityAssessment],
        readiness_assessments: List[ReadinessAssessment],
        job_context: dict,
        favorable_observations: int = 99,
        cooldown_remaining_seconds: float = 0.0,
    ) -> PolicyDecision:
        if not candidate_analyses:
            return PolicyDecision(
                decision=Decision.STAY,
                regime=MigrationRegime.ARBITRAGE,
                reason="no_candidates",
                target_candidate_id=None,
                confidence=1.0,
                metadata={},
                policy_version=self.config.policy_version,
            )

        best = min(candidate_analyses, key=lambda x: x.cost_breakdown.expected_total_cost)
        workload_type = job_context.get("workload_type", "medium").lower()
        remaining = workload_estimate.remaining_runtime_seconds or 0

        # ADR-007 short-job bands.
        if remaining and remaining < self.config.short_runtime_seconds:
            return PolicyDecision(
                decision=Decision.STAY,
                regime=MigrationRegime.ARBITRAGE,
                reason="short_job_no_arbitrage",
                target_candidate_id=None,
                confidence=0.9,
                metadata={"remaining_runtime_seconds": remaining},
                policy_version=self.config.policy_version,
            )
        if workload_type == "short":
            return PolicyDecision(
                decision=Decision.STAY,
                regime=MigrationRegime.ARBITRAGE,
                reason="workload_type_short_no_migrate",
                target_candidate_id=None,
                confidence=1.0,
                metadata={"workload_type": workload_type},
                policy_version=self.config.policy_version,
            )

        threshold = self.config.workload_thresholds.get(workload_type, 0.12)
        threshold = max(threshold, self.config.min_savings_margin)

        savings_ratio = (
            best.cost_breakdown.expected_savings
            / max(stay_analysis.cost_breakdown.expected_total_cost, 0.001)
        )

        completion_overhead = (
            best.cost_breakdown.completion_time_delta_seconds
            / max(remaining or 1, 1)
        )

        # 20% hard ceiling (ADR-006).
        if completion_overhead > self.config.max_completion_overhead_ratio:
            return PolicyDecision(
                decision=Decision.STAY,
                regime=MigrationRegime.ARBITRAGE,
                reason="overhead_ceiling_exceeded",
                target_candidate_id=None,
                confidence=0.85,
                metadata={
                    "completion_overhead_ratio": completion_overhead,
                    "ceiling": self.config.max_completion_overhead_ratio,
                },
                policy_version=self.config.policy_version,
            )

        # ADR-007 MEDIUM band: net_benefit >= max(2*migration_cost, 20%*remaining_savings).
        if remaining and self.config.short_runtime_seconds <= remaining < self.config.medium_runtime_seconds:
            migration_cost = best.cost_breakdown.total_migration_cost
            remaining_savings = max(
                stay_analysis.cost_breakdown.expected_total_cost
                - best.cost_breakdown.candidate_cost_per_hour
                * (remaining / 3600) if hasattr(best.cost_breakdown, "candidate_cost_per_hour") else 0.0,
                0.0,
            )
            net_benefit = best.cost_breakdown.expected_savings
            required = max(
                migration_cost * self.config.medium_net_benefit_multiplier,
                remaining_savings * self.config.medium_required_savings_fraction,
            )
            if net_benefit < required:
                return PolicyDecision(
                    decision=Decision.STAY,
                    regime=MigrationRegime.ARBITRAGE,
                    reason="medium_band_insufficient_net_benefit",
                    target_candidate_id=None,
                    confidence=0.8,
                    metadata={"net_benefit": net_benefit, "required": required},
                    policy_version=self.config.policy_version,
                )

        # Hysteresis: persistence + cooldown (ADR-006).
        if favorable_observations < self.config.min_favorable_observations:
            return PolicyDecision(
                decision=Decision.DEFER,
                regime=MigrationRegime.ARBITRAGE,
                reason="awaiting_favorable_observations",
                target_candidate_id=None,
                confidence=0.7,
                metadata={
                    "favorable_observations": favorable_observations,
                    "required": self.config.min_favorable_observations,
                },
                policy_version=self.config.policy_version,
            )
        if cooldown_remaining_seconds > 0:
            return PolicyDecision(
                decision=Decision.DEFER,
                regime=MigrationRegime.ARBITRAGE,
                reason="cooldown_active",
                target_candidate_id=None,
                confidence=0.9,
                metadata={"cooldown_remaining_seconds": cooldown_remaining_seconds},
                policy_version=self.config.policy_version,
            )

        if (
            best.cost_breakdown.expected_savings > 0
            and savings_ratio >= threshold
        ):
            compat = next(
                (c for c in compatibility_assessments if c.pool_id == best.target_pool_id),
                None,
            )
            ready = next(
                (r for r in readiness_assessments if r.pool_id == best.target_pool_id),
                None,
            )
            if compat and ready and compat.status.value == "COMPATIBLE" and ready.status.value == "READY":
                conf = min(
                    float(best.confidence),
                    float(workload_estimate.prediction_confidence or 0.0),
                    1.0 if (compat.dimensions and compat.dimensions[0].status.value == "COMPATIBLE") else 0.0,
                )
                return PolicyDecision(
                    decision=Decision.MIGRATE,
                    regime=MigrationRegime.ARBITRAGE,
                    reason="economic_benefit",
                    target_candidate_id=best.target_pool_id,
                    confidence=conf,
                    metadata={
                        "savings_ratio": savings_ratio,
                        "completion_overhead_ratio": completion_overhead,
                        "threshold": threshold,
                    },
                    policy_version=self.config.policy_version,
                )

        return PolicyDecision(
            decision=Decision.STAY,
            regime=MigrationRegime.ARBITRAGE,
            reason="insufficient_savings_or_overhead",
            target_candidate_id=None,
            confidence=0.8,
            metadata={
                "best_savings_ratio": savings_ratio if candidate_analyses else 0,
                "threshold": threshold,
                "completion_overhead_ratio": completion_overhead,
            },
            policy_version=self.config.policy_version,
        )


class ProactivePolicy:
    def __init__(self, config: ProactivePolicyConfig = None):
        self.config = config or ProactivePolicyConfig()

    def decide(
        self,
        stay_analysis: CostAnalysis,
        candidate_analyses: list[CostAnalysis],
        workload_estimate: WorkloadEstimate,
        compat_assessments: List[CompatibilityAssessment],
        ready_assessments: List[ReadinessAssessment],
        risk_context: dict,
    ) -> PolicyDecision:
        current_risk = risk_context.get("interruption_probability", 0.0)
        if current_risk < self.config.risk_threshold:
            return PolicyDecision(
                decision=Decision.STAY,
                regime=MigrationRegime.PROACTIVE,
                reason="risk_below_threshold",
                target_candidate_id=None,
                confidence=0.8,
                metadata={"current_risk": current_risk, "threshold": self.config.risk_threshold},
            )

        if not candidate_analyses:
            return PolicyDecision(
                decision=Decision.STAY,
                regime=MigrationRegime.PROACTIVE,
                reason="no_safe_candidates",
                target_candidate_id=None,
                confidence=1.0,
                metadata={},
            )

        ready_candidates = [r for r in ready_assessments if r.status.value == "READY"]
        compat_map = {c.pool_id: c for c in compat_assessments}

        if not ready_candidates:
            return PolicyDecision(
                decision=Decision.STAY,
                regime=MigrationRegime.PROACTIVE,
                reason="no_ready_candidates",
                target_candidate_id=None,
                confidence=1.0,
                metadata={},
            )

        ready_analyses = [a for a in candidate_analyses if a.target_pool_id in {r.pool_id for r in ready_candidates}]

        if not ready_analyses:
            return PolicyDecision(
                decision=Decision.STAY,
                regime=MigrationRegime.PROACTIVE,
                reason="no_ready_analyses",
                target_candidate_id=None,
                confidence=1.0,
                metadata={},
            )

        best = min(ready_analyses, key=lambda x: x.cost_breakdown.expected_total_cost)

        return PolicyDecision(
            decision=Decision.MIGRATE,
            regime=MigrationRegime.PROACTIVE,
            reason="risk_mitigation",
            target_candidate_id=best.target_pool_id,
            confidence=0.7,
            metadata={
                "current_risk": current_risk,
                "threshold": self.config.risk_threshold,
                "expected_savings": best.cost_breakdown.expected_savings,
            },
        )


class RecoveryPolicy:
    """V2 recovery strategy selector.

    `decide_recovery()` is the normative entry point returning a
    models_v2.RecoveryDecision (ADR-008). Legacy `decide()` is kept as a
    thin shim mapping to PolicyDecision(RECOVER/ABANDON) for `--engine v1`
    compat until main.py migrates to the Coordinator path.
    """

    def __init__(self, config: RecoveryPolicyConfig = None):
        self.config = config or RecoveryPolicyConfig()

    def decide_recovery(
        self,
        *,
        job_id: str,
        migration_id: str,
        execution_epoch: int,
        recovery_feasibility,
        compat_assessment=None,
        ready_assessment=None,
        checkpoint_durable: bool = False,
        checkpoint_id: Optional[str] = None,
        lineage_id: Optional[str] = None,
        reconciliation_context: str = "NONE",
        policy_version: str = "v3",
    ):
        from orchestrator.models_v2 import RecoveryDecision as V2RecoveryDecision
        from orchestrator.models_v2 import RecoveryDecisionKind

        if reconciliation_context in ("OWNERSHIP_UNCERTAIN", "SPLIT_BRAIN"):
            return V2RecoveryDecision(
                decision_id=f"rec-{uuid.uuid4().hex[:12]}",
                job_id=job_id, migration_id=migration_id,
                execution_epoch=execution_epoch,
                decision=RecoveryDecisionKind.FAILED,
                reason_code="RECONCILIATION_REQUIRED",
                failure_code="OWNERSHIP_UNCERTAIN",
                replan_required=False,
                checkpoint_id=checkpoint_id, lineage_id=lineage_id,
                confidence_level="LOW",
                confidence_basis={"gate": "reconciliation"},
                policy_version=policy_version,
            )
        status = getattr(getattr(recovery_feasibility, "status", None), "value", None) \
            or str(getattr(recovery_feasibility, "status", "INSUFFICIENT_EVIDENCE"))
        conf = float(getattr(recovery_feasibility, "feasibility_confidence", 0.0) or 0.0)
        if status == "FEASIBLE" and conf >= self.config.min_feasibility_confidence:
            if compat_assessment is not None and ready_assessment is not None:
                if not (compat_assessment.status.value == "COMPATIBLE"
                        and ready_assessment.status.value == "READY"):
                    return V2RecoveryDecision(
                        decision_id=f"rec-{uuid.uuid4().hex[:12]}",
                        job_id=job_id, migration_id=migration_id,
                        execution_epoch=execution_epoch,
                        decision=RecoveryDecisionKind.FAILED,
                        reason_code="NO_VIABLE_CANDIDATE",
                        failure_code="CANDIDATE_NOT_READY",
                        replan_required=True,
                        checkpoint_id=checkpoint_id, lineage_id=lineage_id,
                        confidence_level="MEDIUM",
                        policy_version=policy_version,
                    )
            cp = getattr(recovery_feasibility, "critical_path_seconds", None)
            return V2RecoveryDecision(
                decision_id=f"rec-{uuid.uuid4().hex[:12]}",
                job_id=job_id, migration_id=migration_id,
                execution_epoch=execution_epoch,
                decision=RecoveryDecisionKind.RETRY_RESTORE if checkpoint_durable
                else RecoveryDecisionKind.FORWARD_RECOVERY,
                reason_code="feasible_recovery",
                replan_required=False,
                checkpoint_id=checkpoint_id, lineage_id=lineage_id,
                critical_path_duration_seconds=cp,
                safety_margin_seconds=float(getattr(recovery_feasibility, "safety_margin_seconds", 30.0) or 30.0),
                confidence_level="HIGH" if conf >= 0.8 else "MEDIUM",
                confidence_basis={"feasibility_confidence": conf},
                policy_version=policy_version,
            )
        if status == "INSUFFICIENT_EVIDENCE" and self.config.allow_cold_restart:
            return V2RecoveryDecision(
                decision_id=f"rec-{uuid.uuid4().hex[:12]}",
                job_id=job_id, migration_id=migration_id,
                execution_epoch=execution_epoch,
                decision=RecoveryDecisionKind.COLD_RESTART,
                reason_code="cold_restart_fallback",
                replan_required=True,
                checkpoint_id=None, lineage_id=lineage_id,
                confidence_level="LOW",
                policy_version=policy_version,
            )
        # ADR-008: missing checkpoint without cold restart → FAILED with
        # RECOVERY_REQUIRED semantics carried in reason (coordinator maps to
        # job RECOVERY_REQUIRED; FAILED only after exhaustion).
        if not checkpoint_durable and not self.config.allow_cold_restart:
            reason = "RECOVERY_REQUIRED_NO_DURABLE_CHECKPOINT"
        else:
            reason = "recovery_infeasible"
        return V2RecoveryDecision(
            decision_id=f"rec-{uuid.uuid4().hex[:12]}",
            job_id=job_id, migration_id=migration_id,
            execution_epoch=execution_epoch,
            decision=RecoveryDecisionKind.FAILED,
            reason_code=reason,
            failure_code=str(getattr(recovery_feasibility, "infeasible_reason", "") or "INFEASIBLE"),
            replan_required=False,
            checkpoint_id=checkpoint_id, lineage_id=lineage_id,
            confidence_level="LOW",
            policy_version=policy_version,
        )

    def decide(
        self,
        recovery_feasibility: RecoveryFeasibility,
        recovery_cost: RecoveryCostAnalysis,
        workload_estimate: WorkloadEstimate,
        compat_assessment: CompatibilityAssessment,
        ready_assessment: ReadinessAssessment,
        checkpoint_durable: bool,
    ) -> PolicyDecision:
        if recovery_feasibility.status.value == "FEASIBLE":
            if recovery_feasibility.feasibility_confidence >= self.config.min_feasibility_confidence:
                return PolicyDecision(
                    decision=Decision.RECOVER,
                    regime=MigrationRegime.EMERGENCY,
                    reason="feasible_recovery",
                    target_candidate_id=compat_assessment.pool_id,
                    confidence=recovery_feasibility.feasibility_confidence,
                    metadata={
                        "critical_path_seconds": recovery_feasibility.critical_path_seconds,
                        "safety_margin_seconds": recovery_feasibility.safety_margin_seconds,
                        "remaining_budget_seconds": recovery_feasibility.remaining_budget_seconds,
                    },
                )

        if recovery_feasibility.status.value == "INSUFFICIENT_EVIDENCE":
            if self.config.allow_cold_restart:
                return PolicyDecision(
                    decision=Decision.RECOVER,
                    regime=MigrationRegime.EMERGENCY,
                    reason="cold_restart_fallback",
                    target_candidate_id=compat_assessment.pool_id,
                    confidence=0.3,
                    metadata={"fallback": "cold_restart"},
                )

        return PolicyDecision(
            decision=Decision.ABANDON,
            regime=MigrationRegime.EMERGENCY,
            reason="recovery_infeasible",
            target_candidate_id=None,
            confidence=0.9,
            metadata={
                "checkpoint_durable": checkpoint_durable,
                "feasibility_status": recovery_feasibility.status.value,
                "infeasible_reason": recovery_feasibility.infeasible_reason,
            },
        )


class PolicyEngine:
    def __init__(
        self,
        arbitrage_config: ArbitragePolicyConfig = None,
        proactive_config: ProactivePolicyConfig = None,
        recovery_config: RecoveryPolicyConfig = None,
    ):
        self.arbitrage = ArbitragePolicy(arbitrage_config)
        self.proactive = ProactivePolicy(proactive_config)
        self.recovery = RecoveryPolicy(recovery_config)

    def decide(
        self,
        regime: MigrationRegime,
        stay_analysis: CostAnalysis,
        candidate_analyses: list[CostAnalysis],
        workload_estimate: WorkloadEstimate,
        compat_assessments: List[CompatibilityAssessment],
        ready_assessments: List[ReadinessAssessment],
        recovery_feasibility: Optional[RecoveryFeasibility] = None,
        recovery_cost: Optional[RecoveryCostAnalysis] = None,
        checkpoint_durable: bool = False,
        job_context: dict = None,
        risk_context: dict = None,
    ) -> PolicyDecision:
        job_context = job_context or {}
        risk_context = risk_context or {}

        if regime == MigrationRegime.EMERGENCY:
            if recovery_feasibility is None:
                return PolicyDecision(
                    decision=Decision.ABANDON,
                    regime=regime,
                    reason="no_feasibility_data",
                    target_candidate_id=None,
                    confidence=0.0,
                    metadata={},
                )
            return self.recovery.decide(
                recovery_feasibility,
                None,
                workload_estimate,
                compat_assessments[0] if compat_assessments else None,
                ready_assessments[0] if ready_assessments else None,
                checkpoint_durable,
            )

        if regime == MigrationRegime.PROACTIVE:
            return self.proactive.decide(
                stay_analysis,
                candidate_analyses,
                workload_estimate,
                compat_assessments,
                ready_assessments,
                risk_context,
            )

        return self.arbitrage.decide(
            stay_analysis,
            candidate_analyses,
            workload_estimate,
            compat_assessments,
            ready_assessments,
            job_context,
        )