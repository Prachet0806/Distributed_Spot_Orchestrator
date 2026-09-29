"""Sprint 3 (Track C5): replan / supersession wiring (§25).

Vehicles: S15 (REPLAN → successor via supersedes_plan_id), S16
(emergency supersedes pre-fence arbitrage), S04 (deadline exhausted
pre-fence → abort/replan path), C-PLAN (hash/pins across links).
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from orchestrator import replan as replan_mod
from orchestrator.checkpoint_manager import (
    CheckpointManager, CheckpointResult, RestoreResult)
from orchestrator.migration_coordinator import MigrationCoordinator
from orchestrator.migration_planner import MigrationPlanner, MigrationState
from orchestrator.policy_engine import PolicyDecision, Decision, MigrationRegime
from orchestrator.provisioner import Provisioner
from orchestrator.transfer_manager import TransferManager
from orchestrator.workload_estimator import WorkloadEstimate
from storage.checkpoint_store import CheckpointStore
from storage.job_registry import JobRegistry


def _est(remaining=7200):
    return WorkloadEstimate(
        job_id="job-1", execution_epoch=0, progress=0.5,
        remaining_runtime_seconds=remaining, expected_completion_at=None,
        prediction_confidence=0.8, checkpoint_size_estimate_bytes=100,
        checkpoint_duration_estimate_seconds=1.0,
        estimated_at=datetime.utcnow(), estimator_version="v",
        model_version="v", observation_snapshot_version="1")


def _decision(regime=MigrationRegime.ARBITRAGE, target="pool-t"):
    return PolicyDecision(decision=Decision.MIGRATE, regime=regime,
                          reason="t", target_candidate_id=target,
                          confidence=0.9, metadata={})


def _registry(tmp_path):
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({"job-1": {
        "job_id": "job-1", "state": "RUNNING", "region": "us-east-1",
        "public_ip": "10.0.0.9", "pid": 4242, "version": 0,
        "execution_epoch": 0, "active_migration_id": None}}))
    return JobRegistry(str(path))


def _dump(job_id, pid, host, timeout=300.0):
    return CheckpointResult(checkpoint_id=f"chk-{job_id}", size_bytes=64,
                            duration_seconds=0.01, digest="sha256:replan",
                            status="LOCAL", job_id=job_id, lineage_id=job_id)


def _restore(checkpoint_id, host=None, timeout=300.0):
    return RestoreResult(checkpoint_id=checkpoint_id, success=True,
                         duration_seconds=0.01, process_id=4242)


class _Storage:
    bucket = "replan-bkt"

    def upload(self, job_id, src=None):
        return f"{job_id}.tar.gz"

    def download(self, job_id, dst=None):
        return None


class _Validator:
    def validate(self, migration_id, job_id, epoch, **kw):
        return {"passed": True, "migration_id": migration_id,
                "job_id": job_id, "epoch": epoch}


class _Cleanup:
    def __init__(self):
        self.calls = []

    def cleanup_migration(self, plan, operations, safety_critical_only=False):
        self.calls.append(safety_critical_only)


def _coordinator(reg, store=None):
    calls = []
    return MigrationCoordinator(
        registry=reg, provisioner=Provisioner(),
        checkpoint_manager=CheckpointManager(
            storage=_Storage(), dump_handler=_dump, restore_handler=_restore,
            checkpoint_store=store),
        transfer_manager=TransferManager(storage=_Storage()),
        validator=_Validator(), cleanup_executor=_Cleanup(),
        plan_store=None, checkpoint_store=store,
        fence_invalidate=lambda p: calls.append("invalidate"),
        fence_terminate=lambda p: calls.append("terminate"),
        fence_verify=lambda p: calls.append("verify") or True,
        snapshot_provider=lambda jid, phase: {
            "progress": 0.5, "cpu_utilization_delta": 0.5,
            "memory_utilization_delta": 0.5, "progress_delta": 0.5},
        step_timeout_seconds=30.0), calls


def _arbitrage_plan(**kw):
    params = dict(job_id="job-1", execution_epoch=0,
                  regime=MigrationRegime.ARBITRAGE,
                  policy_decision=_decision(),
                  source_pool_id="pool-s", target_pool_id="pool-t",
                  workload_estimate=_est())
    params.update(kw)
    return MigrationPlanner().create_plan(**params)


# -- signals (S04) --
def test_expired_plan_signals_plan_expired(tmp_path):
    reg = _registry(tmp_path)
    coord, _ = _coordinator(reg)
    plan = _arbitrage_plan()
    plan.expires_at = datetime.utcnow() - timedelta(seconds=1)
    out = coord.execute_plan(plan)
    assert out.current_state == MigrationState.FAILED
    assert out.replan_requested == "plan-expired"
    assert reg.get("job-1")["execution_epoch"] == 0  # ownership untouched


def test_deadline_drift_signals_pre_fence(tmp_path):
    reg = _registry(tmp_path)
    coord, fence_calls = _coordinator(reg)
    plan = MigrationPlanner().create_plan(
        job_id="job-1", execution_epoch=0, regime=MigrationRegime.EMERGENCY,
        policy_decision=_decision(MigrationRegime.EMERGENCY),
        source_pool_id="pool-s", target_pool_id="pool-t",
        workload_estimate=_est(),
        absolute_deadline=datetime.now(timezone.utc) + timedelta(seconds=30))
    out = coord.execute_plan(plan)
    assert out.current_state == MigrationState.FAILED
    assert out.replan_requested == "deadline-drift"
    assert out.replan_context["phase"] == "PRECHECKING"
    assert fence_calls == []  # nothing fenced on the drift path
    job = reg.get("job-1")
    assert job["state"] == "RUNNING"  # full rollback, no durable checkpoint
    assert job["execution_epoch"] == 0


# -- signal gating (post-fence never replans) --
def test_signal_gating_matrix(tmp_path):
    reg = _registry(tmp_path)
    coord, _ = _coordinator(reg)
    plan = _arbitrage_plan()

    plan_ok = _arbitrage_plan()
    out_ok = coord.execute_plan(plan_ok)
    assert out_ok.current_state == MigrationState.SUCCESS
    assert replan_mod.replan_signal_for(out_ok) is None  # success stands

    assert replan_mod.replan_signal_for(None) is None

    failed_no_signal = _arbitrage_plan()
    out = coord.execute_plan(failed_no_signal)
    assert replan_mod.replan_signal_for(out) is None

    # Post-fence outcomes never replan, even with a stale signal present.
    out.fencing_confirmed = True
    out.replan_requested = "deadline-drift"
    assert replan_mod.replan_signal_for(out) is None

    # Recovery replan_required honored pre-fence only.
    out.fencing_confirmed = False
    out.replan_requested = None
    assert replan_mod.replan_signal_for(
        out, recovery_replan_required=True) == "recovery-requested-replan"
    out.fencing_confirmed = True
    assert replan_mod.replan_signal_for(
        out, recovery_replan_required=True) is None


def test_aborted_with_signal_replans():
    from orchestrator.migration_coordinator import MigrationExecutionState
    state = MigrationExecutionState(
        plan=_arbitrage_plan(), current_state=MigrationState.ABORTED,
        state_entered_at=datetime.utcnow(), execution_epoch=0,
        expected_epoch=0, replan_requested="plan-expired")
    assert replan_mod.replan_signal_for(state) == "plan-expired"


# -- budget --
def test_budget_bounds_attempts():
    counts = {}
    assert replan_mod.budget_remaining(counts, "m-1", 2) is True
    assert replan_mod.note_replan(counts, "m-1") == 1
    assert replan_mod.note_replan(counts, "m-1") == 2
    assert replan_mod.budget_remaining(counts, "m-1", 2) is False
    assert replan_mod.budget_remaining(counts, "m-other", 2) is True


def test_max_replans_from_baseline():
    assert replan_mod.max_replans_from_baseline(None) == 2
    assert replan_mod.max_replans_from_baseline({"retry": {"max_replans": 5}}) == 5
    assert replan_mod.max_replans_from_baseline({"retry": "garbage"}) == 2


# -- S15: successor executes to SUCCESS, epoch+1 exactly once --
def test_s15_expired_plan_replans_to_success(tmp_path):
    reg = _registry(tmp_path)
    store = CheckpointStore()
    coord, fence_calls = _coordinator(reg, store)
    planner = MigrationPlanner()

    plan = planner.create_plan(
        job_id="job-1", execution_epoch=0, regime=MigrationRegime.ARBITRAGE,
        policy_decision=_decision(), source_pool_id="pool-s",
        target_pool_id="pool-t", workload_estimate=_est(),
        migration_id="mig-s15")
    plan.pid = 4242
    plan.source_host = "10.0.0.9"
    plan.expires_at = datetime.utcnow() - timedelta(seconds=1)
    out = coord.execute_plan(plan)
    assert out.current_state == MigrationState.FAILED

    # Main-loop replan path (same shape as orchestrator/main.py).
    signal = replan_mod.replan_signal_for(out)
    assert signal == "plan-expired"
    counts = {}
    assert replan_mod.budget_remaining(counts, plan.migration_id, 2)
    replan_mod.note_replan(counts, plan.migration_id)
    successor = planner.create_successor_plan(plan, f"{signal}#1", _est())
    assert successor.migration_id == plan.migration_id  # same attempt
    assert successor.supersedes_plan_id == plan.plan_id
    assert successor.plan_id != plan.plan_id
    assert "replan:plan-expired#1" in successor.authorization_reference
    successor.pid = 4242
    successor.source_host = "10.0.0.9"
    out2 = coord.execute_plan(successor)
    assert out2.current_state == MigrationState.SUCCESS
    assert fence_calls == ["invalidate", "terminate", "verify"]
    job = reg.get("job-1")
    assert job["state"] == "RUNNING"
    assert job["execution_epoch"] == 1  # ownership moved exactly once
    assert job["active_migration_id"] is None


# -- S16: emergency pre-emption --
def test_s16_preempt_builds_emergency_successor():
    planner = MigrationPlanner()
    old = planner.create_plan(
        job_id="job-1", execution_epoch=0, regime=MigrationRegime.ARBITRAGE,
        policy_decision=_decision(), source_pool_id="pool-s",
        target_pool_id="pool-t", workload_estimate=_est(),
        migration_id="mig-s16")
    deadline = datetime.now(timezone.utc) + timedelta(seconds=600)
    successor = replan_mod.preempt_with_emergency(
        planner, old, workload_estimate=_est(),
        policy_decision=_decision(MigrationRegime.EMERGENCY),
        absolute_deadline=deadline, target_pool_id="pool-e")
    assert successor.regime == MigrationRegime.EMERGENCY
    assert successor.migration_id == old.migration_id
    assert successor.supersedes_plan_id == old.plan_id
    assert successor.target_pool_id == "pool-e"
    assert successor.absolute_deadline == deadline
    assert successor.plan_hash != old.plan_hash


def test_s16_refuses_post_fence_and_non_arbitrage():
    planner = MigrationPlanner()
    old = _arbitrage_plan()
    with pytest.raises(ValueError, match="post-fencing"):
        replan_mod.preempt_with_emergency(
            planner, old, workload_estimate=_est(), fencing_confirmed=True)
    emergency = MigrationPlanner().create_plan(
        job_id="job-1", execution_epoch=0, regime=MigrationRegime.EMERGENCY,
        policy_decision=_decision(MigrationRegime.EMERGENCY),
        source_pool_id="pool-s", target_pool_id="pool-t",
        workload_estimate=_est(),
        absolute_deadline=datetime.now(timezone.utc) + timedelta(seconds=600))
    with pytest.raises(ValueError, match="already EMERGENCY"):
        replan_mod.preempt_with_emergency(
            planner, emergency, workload_estimate=_est())


def test_s16_live_supersede_with_regime_override(tmp_path):
    from orchestrator.migration_coordinator import MigrationExecutionState
    reg = _registry(tmp_path)
    cleanup = _Cleanup()
    coord = MigrationCoordinator(
        registry=reg, provisioner=Provisioner(),
        checkpoint_manager=CheckpointManager(storage=_Storage()),
        transfer_manager=TransferManager(storage=_Storage()),
        validator=_Validator(), cleanup_executor=cleanup)
    planner = MigrationPlanner()
    old = planner.create_plan(
        job_id="job-1", execution_epoch=0, regime=MigrationRegime.ARBITRAGE,
        policy_decision=_decision(), source_pool_id="pool-s",
        target_pool_id="pool-t", workload_estimate=_est(),
        migration_id="mig-live-s16")
    coord._execution_state = MigrationExecutionState(
        plan=old, current_state=MigrationState.CHECKPOINTING,
        state_entered_at=datetime.utcnow(), execution_epoch=0,
        expected_epoch=0)
    coord._start_mono = coord._monotonic()
    deadline = datetime.now(timezone.utc) + timedelta(seconds=600)
    successor = coord.supersede_and_replan(
        planner, "emergency-preempts", workload_estimate=_est(),
        regime=MigrationRegime.EMERGENCY, absolute_deadline=deadline)
    assert successor.regime == MigrationRegime.EMERGENCY
    assert successor.supersedes_plan_id == old.plan_id
    assert coord._execution_state.current_state == MigrationState.SUPERSEDED
    assert cleanup.calls == [True]  # safety-critical cleanup only (§6.7)
