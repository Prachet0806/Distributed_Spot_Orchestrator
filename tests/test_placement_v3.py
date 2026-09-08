"""Phase 1b: placement v3 — normalization, tie-break, expiry, compat inputs."""
from datetime import datetime

from orchestrator.candidate_placement import (
    PlacementEngine, PlacementPolicy, PlacementInput, PLACEMENT_V3_WEIGHTS,
)
from orchestrator.candidate_compatibility import CompatibilityStatus
from orchestrator.candidate_readiness import ReadinessStatus
from orchestrator.cost_risk_evaluator import CostRiskEvaluator
from orchestrator.workload_estimator import WorkloadEstimate


def _inp(pool, compat=CompatibilityStatus.COMPATIBLE,
         ready=ReadinessStatus.READY, total_cost=5.0, risk=0.2, cap=0.9):
    return PlacementInput(
        pool_id=pool, compatibility_status=compat, readiness_status=ready,
        compatibility_dimensions={"cpu": True, "mem": True},
        readiness_checks=[{"status": "READY"}],
        cost_analysis={"expected_total_cost": total_cost},
        risk_analysis={"interruption_probability": risk},
        topology={}, capacity_confidence=cap,
    )


def test_cheaper_candidate_wins_and_expiry_in_future():
    eng = PlacementEngine(PlacementPolicy(version="v3"))
    rec = eng.rank([_inp("pool-b", total_cost=9.0), _inp("pool-a", total_cost=3.0)])
    assert rec.selected_candidate_id == "pool-a"
    assert rec.expires_at > rec.generated_at
    assert rec.policy_version == "v3"


def test_final_tie_break_is_pool_id_lexicographic():
    eng = PlacementEngine(PlacementPolicy(version="v3"))
    rec = eng.rank([_inp("pool-b"), _inp("pool-a")])
    assert [r.pool_id for r in rec.ranked_candidates] == ["pool-a", "pool-b"]
    assert rec.selected_candidate_id == "pool-a"


def test_ineligible_filtered_before_ranking():
    eng = PlacementEngine(PlacementPolicy(version="v3"))
    rec = eng.rank([
        _inp("pool-bad", compat=CompatibilityStatus.INCOMPATIBLE, total_cost=0.01),
        _inp("pool-good", total_cost=100.0),
    ])
    assert rec.selected_candidate_id == "pool-good"


def test_accepts_cost_analysis_objects():
    ev = CostRiskEvaluator()
    est = WorkloadEstimate(job_id="j", execution_epoch=1, progress=0.5,
                           remaining_runtime_seconds=3600,
                           expected_completion_at=None, prediction_confidence=0.8,
                           checkpoint_size_estimate_bytes=100,
                           checkpoint_duration_estimate_seconds=1.0,
                           estimated_at=datetime.utcnow(), estimator_version="v",
                           model_version="v", observation_snapshot_version="1")
    ca = ev.evaluate_candidate(0.01, 0.002, est, 0.1)
    eng = PlacementEngine(PlacementPolicy(version="v3"))
    inp = _inp("pool-a")
    inp.cost_analysis = ca
    rec = eng.rank([inp])
    assert rec.selected_candidate_id == "pool-a"


def test_v21_file_translates_to_v3_weights():
    pol = PlacementPolicy.load_from_file("config/placement_policy.yaml")
    assert set(pol.ranking_weights) == set(PLACEMENT_V3_WEIGHTS)
    assert abs(sum(pol.ranking_weights.values()) - 1.0) < 1e-9
