"""Phase 1a: V2 domain contracts, transition tables, frozen baseline."""
from datetime import datetime, timedelta

from orchestrator.models_v2 import (
    MigrationPlanV2, PlanStep, PlanStepType, RollbackClass,
    RecoveryDecision, CheckpointRef, ValidationReport, ReconciliationFinding,
    canonical_plan_dict, compute_plan_hash, STEP_ROLLBACK,
)
from orchestrator.config_loader import load_v2_baseline
from storage.v2_transitions import is_v2_transition_allowed


def _plan():
    return MigrationPlanV2(
        plan_id="p1", job_id="j1", migration_id="m1", regime="ARBITRAGE",
        source_pool_id="s", target_pool_id="t",
        created_at="2026-01-01T00:00:00", expires_at="2026-01-01T00:10:00",
        input_snapshot_versions={"policy": "v3"},
        authorization_reference="dec-1",
        steps=[PlanStep(step_id="a", type=PlanStepType.CHECKPOINT,
                        operation_type="CreateCheckpoint",
                        rollback_class=STEP_ROLLBACK["CHECKPOINT"])],
    )


def test_plan_hash_stable_and_excludes_volatile():
    p1, p2 = _plan(), _plan()
    p2.created_at = "2026-06-06T06:06:06"  # volatile: must not affect hash
    assert compute_plan_hash(p1) == compute_plan_hash(p2)
    assert "created_at" not in canonical_plan_dict(p1)


def test_plan_hash_changes_on_semantic_edit():
    p1, p2 = _plan(), _plan()
    p2.target_pool_id = "other"
    assert compute_plan_hash(p1) != compute_plan_hash(p2)


def test_plan_expiry():
    p = _plan()
    assert p.is_expired(now=datetime(2026, 1, 1, 0, 11, 0)) is True
    assert p.is_expired(now=datetime(2026, 1, 1, 0, 5, 0)) is False


def test_fence_is_forward_only():
    assert STEP_ROLLBACK["FENCE"] == RollbackClass.FORWARD_ONLY


def test_v2_job_transitions():
    assert is_v2_transition_allowed("job", "RUNNING", "MIGRATING")
    assert is_v2_transition_allowed("job", "RUNNING", "RECOVERY_REQUIRED")
    assert not is_v2_transition_allowed("job", "RUNNING", "PLANNED")
    assert not is_v2_transition_allowed("job", "COMPLETED", "RUNNING")


def test_v2_no_abort_mid_fence():
    assert not is_v2_transition_allowed("migration", "FENCING", "ABORTED")
    assert is_v2_transition_allowed("migration", "FENCING", "VALIDATING")
    assert is_v2_transition_allowed("migration", "RESTORING", "ABORTED")


def test_v2_baseline_loads_frozen_values():
    base = load_v2_baseline()
    assert base["policy"]["cooldown_seconds"] == 900
    assert base["policy"]["min_favorable_observations"] == 3
    assert base["recovery"]["allow_cold_restart"] is False
    assert base["concurrency"]["max_concurrent_migrations"] == 3
    assert base["transfer"]["multipart_part_size_bytes"] == 67108864
