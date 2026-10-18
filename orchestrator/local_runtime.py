"""Explicit credential-free wiring for the same production iteration function."""
from types import SimpleNamespace
import logging
from orchestrator.runtime_context import RuntimeContext
from orchestrator.control_plane_lease import check_lease_for_iteration
from orchestrator import interruption_ingestion, replan
from orchestrator.workload_estimator import WorkloadEstimator, EstimatorConfig
from orchestrator.candidate_compatibility import CompatibilityEngine
from orchestrator.candidate_readiness import ReadinessEngine
from orchestrator.candidate_placement import PlacementEngine, PlacementPolicy
from orchestrator.recovery_feasibility import RecoveryFeasibilityEngine
from orchestrator.cost_risk_evaluator import CostRiskEvaluator
from orchestrator.policy_engine import PolicyEngine, ArbitragePolicyConfig
from orchestrator.migration_planner import MigrationPlanner
from orchestrator.migration_history import MigrationHistory
from orchestrator.rate_limiter import TokenBucketRateLimiter
from pathlib import Path


def create_context(*, registry, watcher, pool_store, plan_store, checkpoint_store,
                   coordinator_factory, provisioner, lease, reconciliation, event_dispatcher,
                   transport_for, readiness_evidence_provider, migrate=False, history_store=None,
                   clock=None, launch_handler=None):
    args = SimpleNamespace(migrate=migrate, multi_job=True, current_region=None, job_id=None,
                           stuck_seconds=3600, check_spot_flag=False,
                           spot_flag_path="/opt/job_workspace/flags/spot_interrupt",
                           cooldown_seconds=900)
    values = dict(args=args, cfg={"dynamodb_region": "local"}, registry=registry, watcher=watcher,
        pool_store=pool_store, plan_store=plan_store, checkpoint_store=checkpoint_store,
        _wired_coordinator=coordinator_factory, _transport_for=transport_for,
        admission_provisioner=provisioner, _cpr_lease=lease,
        check_lease_for_iteration=check_lease_for_iteration, reconciliation=reconciliation,
        event_dispatcher=event_dispatcher, _ingestion=interruption_ingestion, _replan_mod=replan,
        _max_replans=2, _replan_counts={}, _seen_interruptions=set(), _persisted_pools=set(),
        _pending_attempt={}, _tele_attempt={}, favorable_streaks={}, last_migration_ts={},
        last_reconcile=0, reconcile_interval=300, price_cache={"ts": 0, "data": None},
        include_states=["PENDING", "RUNNING", "RECOVERY_REQUIRED", "RECONCILIATION_REQUIRED"],
        instance_type="t3.micro", log=logging.getLogger("local-runtime"),
        estimator=WorkloadEstimator(EstimatorConfig()), compatibility_engine=CompatibilityEngine(),
        readiness_engine=ReadinessEngine(), feasibility_engine=RecoveryFeasibilityEngine(),
        evaluator=CostRiskEvaluator(), planner=MigrationPlanner(),
        policy=PolicyEngine(ArbitragePolicyConfig(min_favorable_observations=1)),
        placement_engine=PlacementEngine(PlacementPolicy.load_from_file(str(
            Path(__file__).resolve().parent.parent / "config/placement_policy.yaml"))),
        history=MigrationHistory(storage=history_store),
        rate_limiter=TokenBucketRateLimiter(max_per_hour=100, max_concurrent=2, min_interval_seconds=0),
        emergency_rate_limiter=TokenBucketRateLimiter(max_per_hour=100, max_concurrent=2, min_interval_seconds=0),
        readiness_evidence_provider=readiness_evidence_provider, launch_handler=launch_handler)
    # The CLI uses this same baseline reader; tests can pin policy independently.
    from orchestrator.config_loader import load_v2_baseline
    values["load_v2_baseline"] = load_v2_baseline
    return RuntimeContext(values, **({"wall_clock": clock} if clock else {}))
