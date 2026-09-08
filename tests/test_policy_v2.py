"""Phase 1b: V2 policy — hysteresis, short-job, overhead ceiling, recovery."""
from datetime import datetime

from orchestrator.policy_engine import (
    ArbitragePolicy, ArbitragePolicyConfig, PolicyEngine, MigrationRegime,
    RecoveryPolicy, RecoveryPolicyConfig,
)
from orchestrator.cost_risk_evaluator import CostRiskEvaluator
from orchestrator.workload_estimator import WorkloadEstimate
from orchestrator.candidate_compatibility import (
    CompatibilityAssessment, CompatibilityStatus, CompatibilityDimensionResult,
)
from orchestrator.candidate_readiness import (
    ReadinessAssessment, ReadinessStatus, ReadinessCheck, CapacityEvidence,
    CapacityStatus, EvidenceSource, IAMReadiness, NetworkReadiness, StorageReadiness,
)
from orchestrator.recovery_feasibility import (
    RecoveryFeasibility, FeasibilityStatus, PlanStepDuration,
)
from orchestrator.models_v2 import RecoveryDecisionKind


def _est(remaining, conf=0.8):
    return WorkloadEstimate(
        job_id="j1", execution_epoch=1, progress=0.5,
        remaining_runtime_seconds=remaining,
        expected_completion_at=None, prediction_confidence=conf,
        checkpoint_size_estimate_bytes=10 * 1024**2,
        checkpoint_duration_estimate_seconds=10.0,
        estimated_at=datetime.utcnow(), estimator_version="v",
        model_version="v", observation_snapshot_version="1",
    )


def _compat(pool_id="pool-a", status=CompatibilityStatus.COMPATIBLE):
    return CompatibilityAssessment(
        assessment_id="c1", pool_id=pool_id,
        workload_requirements_version="1", status=status,
        dimensions=[CompatibilityDimensionResult("cpu", status)],
        assessed_at=datetime.utcnow(),
    )


def _ready(pool_id="pool-a", status=ReadinessStatus.READY):
    return ReadinessAssessment(
        assessment_id="r1", pool_id=pool_id, status=status,
        checks=[ReadinessCheck("iam", status)],
        capacity_evidence=CapacityEvidence(
            status=CapacityStatus.AVAILABLE,
            source=EvidenceSource.HISTORICAL_PROVISIONING,
            observed_at=datetime.utcnow(), confidence=0.9,
            provisioning_success_rate=0.9, sample_count=10),
        iam_readiness=IAMReadiness(True, True, True, True),
        network_readiness=NetworkReadiness(True, True, True, True),
        storage_readiness=StorageReadiness(True, True, True),
        artifact_available=True, runtime_ready=True,
    )


def _analyses(savings=5.0, total=5.0, delta=60.0, pool="pool-a", conf=0.9):
    ev = CostRiskEvaluator()
    est = _est(7200)
    stay = ev.evaluate_stay(0.01, est, 0.05)
    cand = ev.evaluate_candidate(0.01, 0.002, est, 0.05,
                                 checkpoint_size_bytes=10 * 1024**2,
                                 checkpoint_duration_seconds=10.0)
    # Override to deterministic economics.
    cand.cost_breakdown.expected_savings = savings
    cand.cost_breakdown.expected_total_cost = total
    cand.cost_breakdown.completion_time_delta_seconds = delta
    cand.target_pool_id = pool
    cand.confidence = conf
    return stay, [cand]


def test_arbitrage_migrate_when_economic_and_hysteresis_met():
    pol = ArbitragePolicy(ArbitragePolicyConfig())
    stay, cands = _analyses(savings=5.0, total=5.0, delta=60.0)
    d = pol.decide(stay, cands, _est(7200), [_compat()], [_ready()],
                   {"workload_type": "long"},
                   favorable_observations=3, cooldown_remaining_seconds=0.0)
    assert d.decision.value == "MIGRATE"
    assert d.target_candidate_id == "pool-a"
    assert d.policy_version == "v3"
    assert d.decision_id


def test_arbitrage_defers_until_favorable_observations():
    pol = ArbitragePolicy(ArbitragePolicyConfig())
    stay, cands = _analyses()
    d = pol.decide(stay, cands, _est(7200), [_compat()], [_ready()],
                   {"workload_type": "long"},
                   favorable_observations=1, cooldown_remaining_seconds=0.0)
    assert d.decision.value == "DEFER"
    assert d.reason == "awaiting_favorable_observations"


def test_arbitrage_defers_on_cooldown():
    pol = ArbitragePolicy(ArbitragePolicyConfig())
    stay, cands = _analyses()
    d = pol.decide(stay, cands, _est(7200), [_compat()], [_ready()],
                   {"workload_type": "long"},
                   favorable_observations=5, cooldown_remaining_seconds=100.0)
    assert d.decision.value == "DEFER"
    assert d.reason == "cooldown_active"


def test_short_job_never_arbitrages():
    pol = ArbitragePolicy(ArbitragePolicyConfig())
    stay, cands = _analyses()
    d = pol.decide(stay, cands, _est(600), [_compat()], [_ready()],
                   {"workload_type": "medium"},
                   favorable_observations=9, cooldown_remaining_seconds=0.0)
    assert d.decision.value == "STAY"
    assert d.reason == "short_job_no_arbitrage"


def test_overhead_ceiling_blocks():
    pol = ArbitragePolicy(ArbitragePolicyConfig())
    stay, cands = _analyses(savings=50.0, total=1.0, delta=3600.0)
    d = pol.decide(stay, cands, _est(3600), [_compat()], [_ready()],
                   {"workload_type": "long"},
                   favorable_observations=9, cooldown_remaining_seconds=0.0)
    assert d.decision.value == "STAY"
    assert d.reason == "overhead_ceiling_exceeded"


def _feas(status=FeasibilityStatus.FEASIBLE, conf=0.9):
    return RecoveryFeasibility(
        status=status, deadline_seconds=120.0, critical_path_seconds=60.0,
        safety_margin_seconds=30.0, remaining_budget_seconds=120.0,
        steps=[PlanStepDuration("checkpoint", 10.0, 0.9)],
        assumptions=[], confidence=conf, feasibility_confidence=conf,
        evaluated_at=datetime.utcnow(),
    )


def test_recovery_feasible_with_durable_is_retry_restore():
    rec = RecoveryPolicy(RecoveryPolicyConfig())
    d = rec.decide_recovery(job_id="j", migration_id="m", execution_epoch=1,
                            recovery_feasibility=_feas(),
                            compat_assessment=_compat(), ready_assessment=_ready(),
                            checkpoint_durable=True, checkpoint_id="chk-1")
    assert d.decision == RecoveryDecisionKind.RETRY_RESTORE
    assert d.checkpoint_id == "chk-1"
    assert d.replan_required is False


def test_recovery_gate_blocks_on_ownership_uncertain():
    rec = RecoveryPolicy(RecoveryPolicyConfig())
    d = rec.decide_recovery(job_id="j", migration_id="m", execution_epoch=1,
                            recovery_feasibility=_feas(),
                            checkpoint_durable=True,
                            reconciliation_context="SPLIT_BRAIN")
    assert d.decision == RecoveryDecisionKind.FAILED
    assert d.reason_code == "RECONCILIATION_REQUIRED"


def test_recovery_no_checkpoint_no_cold_restart():
    rec = RecoveryPolicy(RecoveryPolicyConfig(allow_cold_restart=False))
    d = rec.decide_recovery(job_id="j", migration_id="m", execution_epoch=1,
                            recovery_feasibility=_feas(FeasibilityStatus.INFEASIBLE, 0.1),
                            checkpoint_durable=False)
    assert d.decision == RecoveryDecisionKind.FAILED
    assert d.reason_code == "RECOVERY_REQUIRED_NO_DURABLE_CHECKPOINT"


def test_recovery_cold_restart_when_allowed():
    rec = RecoveryPolicy(RecoveryPolicyConfig(allow_cold_restart=True))
    d = rec.decide_recovery(
        job_id="j", migration_id="m", execution_epoch=1,
        recovery_feasibility=_feas(FeasibilityStatus.INSUFFICIENT_EVIDENCE, 0.0),
        checkpoint_durable=False)
    assert d.decision == RecoveryDecisionKind.COLD_RESTART
    assert d.replan_required is True


def test_engine_emergency_shim_still_returns_policy_decision():
    eng = PolicyEngine()
    stay, cands = _analyses()
    d = eng.decide(MigrationRegime.EMERGENCY, stay, cands, _est(7200),
                   [_compat()], [_ready()],
                   recovery_feasibility=_feas(), recovery_cost=None,
                   checkpoint_durable=True,
                   job_context={}, risk_context={})
    assert d.decision.value in ("RECOVER", "ABANDON")
