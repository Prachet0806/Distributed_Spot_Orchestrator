"""Phase 3: execution kernel — planner DAG, coordinator safety, executors."""
import json
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from orchestrator.migration_planner import MigrationPlanner, MigrationState
from orchestrator.migration_coordinator import (
    MigrationCoordinator, MigrationFailed, OperationUnknownError,
)
from orchestrator.policy_engine import PolicyDecision, Decision, MigrationRegime
from orchestrator.workload_estimator import WorkloadEstimate
from orchestrator.checkpoint_manager import CheckpointManager, CheckpointResult, RestoreResult
from orchestrator.provisioner import Provisioner
from orchestrator.transfer_manager import TransferManager
from orchestrator.models_v2 import PlanStepType
from storage.job_registry import JobRegistry
from storage.plan_store import PlanStore
from storage.checkpoint_store import CheckpointStore


def _est(remaining=7200):
    return WorkloadEstimate(
        job_id="job-1", execution_epoch=0, progress=0.5,
        remaining_runtime_seconds=remaining, expected_completion_at=None,
        prediction_confidence=0.8, checkpoint_size_estimate_bytes=100,
        checkpoint_duration_estimate_seconds=1.0,
        estimated_at=datetime.utcnow(), estimator_version="v",
        model_version="v", observation_snapshot_version="1")


def _decision():
    return PolicyDecision(decision=Decision.MIGRATE, regime=MigrationRegime.ARBITRAGE,
                          reason="t", target_candidate_id="pool-t",
                          confidence=0.9, metadata={})


def _registry(tmp_path):
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({"job-1": {
        "job_id": "job-1", "state": "RUNNING", "region": "us-east-1",
        "public_ip": "10.0.0.9", "pid": 4242, "version": 0,
        "execution_epoch": 0, "active_migration_id": None}}))
    return JobRegistry(str(path))


def _dump(job_id, pid, host, timeout=300.0):
    return CheckpointResult(checkpoint_id=f"chk-{job_id}", size_bytes=100,
                            duration_seconds=0.01, digest="sha256:fake",
                            status="LOCAL", job_id=job_id, lineage_id=job_id)


def _restore(checkpoint_id, host=None, timeout=300.0):
    return RestoreResult(checkpoint_id=checkpoint_id, success=True,
                         duration_seconds=0.01, process_id=999)


class _Storage:
    def __init__(self):
        self.uploaded = []
        self.downloaded = []
        self.bucket = "bkt"

    def upload(self, job_id, src=None):
        self.uploaded.append(job_id)
        return f"{job_id}.tar.gz"

    def download(self, job_id, dst=None):
        self.downloaded.append(job_id)


class _Validator:
    def __init__(self, passed=True):
        self._passed = passed
        self.calls = 0

    def validate(self, migration_id, job_id, epoch, **kw):
        self.calls += 1
        return {"passed": self._passed, "migration_id": migration_id,
                "job_id": job_id, "epoch": epoch}


class _Cleanup:
    def __init__(self):
        self.calls = []

    def cleanup_migration(self, plan, operations, safety_critical_only=False):
        self.calls.append((plan.migration_id, len(operations),
                           safety_critical_only))


def _fences(calls):
    return {
        "invalidate": lambda plan: calls.append("invalidate"),
        "terminate": lambda plan: calls.append("terminate"),
        "verify": lambda plan: calls.append("verify") or True,
    }


def _coordinator(reg, fences=None, validator=None, checkpoint_store=None,
                 plan_store=None, transfer_storage=None, provisioner=None,
                 **kw):
    calls = []
    fh = fences if fences is not None else _fences(calls)
    return MigrationCoordinator(
        registry=reg,
        provisioner=provisioner or Provisioner(),
        checkpoint_manager=CheckpointManager(
            storage=_Storage(), dump_handler=_dump, restore_handler=_restore,
            checkpoint_store=checkpoint_store),
        transfer_manager=TransferManager(storage=transfer_storage or _Storage()),
        validator=validator or _Validator(),
        cleanup_executor=_Cleanup(),
        plan_store=plan_store,
        checkpoint_store=checkpoint_store,
        fence_invalidate=fh["invalidate"], fence_terminate=fh["terminate"],
        fence_verify=fh["verify"],
        step_timeout_seconds=kw.pop("step_timeout_seconds", 30.0),
        **kw), calls


def _plan(regime=MigrationRegime.ARBITRAGE, **kw):
    planner = MigrationPlanner()
    return planner.create_plan(
        job_id="job-1", execution_epoch=0, regime=regime,
        policy_decision=_decision(), source_pool_id="pool-s",
        target_pool_id="pool-t", workload_estimate=_est(), **kw)


# -- planner --
def test_planner_hash_ttl_and_fencing_authorized():
    p1 = _plan()
    p2 = _plan()
    assert p1.plan_hash and p1.plan_hash != p2.plan_hash  # unique migration_id
    assert p1.plan_id.startswith("plan-")
    ttl = (p1.expires_at - p1.created_at).total_seconds()
    assert ttl == pytest.approx(600.0)
    assert p1.fencing_authorized is True  # all regimes fence (ADR)
    assert p1.is_expired() is False
    assert p1.is_expired(now=datetime.utcnow() + timedelta(hours=2)) is True


def test_planner_emergency_dag_parallel(tmp_path):
    plan = _plan(regime=MigrationRegime.EMERGENCY,
                 absolute_deadline=datetime.utcnow() + timedelta(seconds=300))
    planner = MigrationPlanner()
    dag = planner.to_v2_dag(plan)
    assert [s.type for s in dag] == [
        PlanStepType.PRECHECK, PlanStepType.CHECKPOINT, PlanStepType.PERSIST,
        PlanStepType.PROVISION, PlanStepType.TRANSFER, PlanStepType.RESTORE,
        PlanStepType.FENCE, PlanStepType.VALIDATE, PlanStepType.ACTIVATE,
        PlanStepType.FINALIZE,
    ]
    by_type = {s.type: s for s in dag}
    prov_id = by_type[PlanStepType.PROVISION].step_id
    persist_id = by_type[PlanStepType.PERSIST].step_id
    precheck_id = by_type[PlanStepType.PRECHECK].step_id
    assert by_type[PlanStepType.PROVISION].depends_on == [precheck_id]
    transfer = by_type[PlanStepType.TRANSFER]
    assert set(transfer.depends_on) == {persist_id, prov_id}
    assert all(s.join_policy == "ALL_SUCCEEDED" for s in dag)
    assert by_type[PlanStepType.FENCE].deadline_behavior == "COMPLETE_FENCING"


def test_planner_serial_dag_chains():
    planner = MigrationPlanner()
    dag = planner.to_v2_dag(_plan())
    for prev, cur in zip(dag, dag[1:]):
        assert cur.depends_on == [prev.step_id]


# -- coordinator happy paths --
def test_arbitrage_happy_path(tmp_path):
    reg = _registry(tmp_path)
    coord, calls = _coordinator(reg)
    plan = _plan()
    out = coord.execute_plan(plan)
    assert out.current_state == MigrationState.SUCCESS
    assert calls == ["invalidate", "terminate", "verify"]
    job = reg.get("job-1")
    assert job["state"] == "RUNNING"
    assert job["execution_epoch"] == 1  # ownership transferred exactly once
    assert job["active_migration_id"] is None
    op_ids = [o.operation_id for o in out.operations]
    assert len(set(op_ids)) == len(op_ids)  # ULID unique
    assert all(o.status == "SUCCEEDED" for o in out.operations)
    assert out.target_instance_id
    assert out.checkpoint_id == "chk-job-1"


def test_emergency_provisions_exactly_once(tmp_path):
    reg = _registry(tmp_path)
    prov = Provisioner()
    calls_provision = []
    orig = prov.provision_with_operation
    def _count(cid, operation_id=None, **kw):
        calls_provision.append(operation_id)
        return orig(cid, operation_id=operation_id, **kw)
    prov.provision_with_operation = _count
    coord, _ = _coordinator(reg, provisioner=prov)
    plan = _plan(regime=MigrationRegime.EMERGENCY,
                 absolute_deadline=datetime.utcnow() + timedelta(seconds=600))
    out = coord.execute_plan(plan)
    assert out.current_state == MigrationState.SUCCESS
    assert len(calls_provision) == 1  # no double-provision regression


def test_expired_plan_fails_without_side_effects(tmp_path):
    reg = _registry(tmp_path)
    coord, calls = _coordinator(reg)
    plan = _plan()
    plan.expires_at = datetime.utcnow() - timedelta(seconds=1)
    out = coord.execute_plan(plan)
    assert out.current_state == MigrationState.FAILED
    assert calls == []
    assert out.operations == []
    assert reg.get("job-1")["state"] == "FAILED"


def test_fencing_fails_closed_without_hooks(tmp_path):
    reg = _registry(tmp_path)
    coord = MigrationCoordinator(
        registry=reg, provisioner=Provisioner(),
        checkpoint_manager=CheckpointManager(
            storage=_Storage(), dump_handler=_dump, restore_handler=_restore),
        transfer_manager=TransferManager(storage=_Storage()),
        validator=_Validator(), cleanup_executor=_Cleanup(),
        step_timeout_seconds=30.0)
    out = coord.execute_plan(_plan())
    assert out.current_state == MigrationState.FAILED
    # FENCING_FAILED → reconciliation gate, never fake success.
    assert reg.get("job-1")["state"] == "RECONCILIATION_REQUIRED"


def test_prefence_failure_rolls_back_to_running(tmp_path):
    reg = _registry(tmp_path)
    def _boom(job_id, pid, host, timeout=300.0):
        raise RuntimeError("criu exploded")
    coord, _ = _coordinator(reg)
    coord.checkpoint_manager.dump_handler = _boom
    out = coord.execute_plan(_plan())
    assert out.current_state == MigrationState.FAILED
    job = reg.get("job-1")
    assert job["state"] == "RUNNING"  # FULL_ROLLBACK, no durable checkpoint
    assert job["execution_epoch"] == 0  # ownership untouched


def test_prefence_failure_with_durable_goes_recovery(tmp_path):
    reg = _registry(tmp_path)
    store = CheckpointStore()
    from orchestrator.models_v2 import CheckpointRef
    store.put(CheckpointRef("chk-job-1", "job-1", 1, 0, durability="DURABLE",
                            integrity_verified=True))
    def _boom(job_id, pid, host, timeout=300.0):
        raise RuntimeError("criu exploded")
    coord, _ = _coordinator(reg, checkpoint_store=store)
    coord.checkpoint_manager.dump_handler = _boom
    out = coord.execute_plan(_plan())
    assert out.current_state == MigrationState.FAILED
    assert reg.get("job-1")["state"] == "RECOVERY_REQUIRED"


def test_postfence_validation_failure_is_forward_only(tmp_path):
    reg = _registry(tmp_path)
    coord, _ = _coordinator(reg, validator=_Validator(passed=False))
    out = coord.execute_plan(_plan())
    assert out.current_state == MigrationState.FAILED
    job = reg.get("job-1")
    assert job["state"] == "RECOVERY_REQUIRED"  # never rollback to source
    assert job["execution_epoch"] == 1  # fenced ownership stands


def test_epoch_drift_blocks_before_fence(tmp_path):
    reg = _registry(tmp_path)
    coord, calls = _coordinator(reg)
    plan = _plan()
    plan.execution_epoch = 99  # stale plan
    out = coord.execute_plan(plan)
    assert out.current_state == MigrationState.FAILED
    assert calls == []  # fenced nothing
    assert reg.get("job-1")["execution_epoch"] == 0


def test_step_timeout_marks_unknown(tmp_path):
    reg = _registry(tmp_path)
    def _slow(job_id, pid, host, timeout=300.0):
        time.sleep(5)
        return _dump(job_id, pid, host)
    coord, _ = _coordinator(reg, step_timeout_seconds=0.2)
    coord.checkpoint_manager.dump_handler = _slow
    out = coord.execute_plan(_plan())
    assert out.current_state == MigrationState.FAILED
    unknowns = [o for o in out.operations if o.status == "UNKNOWN"]
    assert unknowns  # timeout ≠ failure: surfaced as UNKNOWN


def test_no_abort_transition_out_of_fencing():
    coord = MigrationCoordinator(
        registry=object(), provisioner=object(),
        checkpoint_manager=object(), transfer_manager=object(),
        validator=object(), cleanup_executor=object())
    sm = coord._state_machine[MigrationState.FENCING]
    assert "abort" not in sm  # COMPLETE_FENCING invariant


# -- executors --
def test_provisioner_idempotent_replay():
    prov = Provisioner()
    r1 = prov.provision_with_operation("pool-x", operation_id="op-1")
    r2 = prov.provision_with_operation("pool-x", operation_id="op-1")
    assert r1.instance_id == r2.instance_id
    assert prov.get_operation_status("op-1")["state"] == "SUCCEEDED"
    assert prov.get_operation_status("op-nope")["state"] == "UNKNOWN"


def test_checkpoint_manager_fails_closed():
    mgr = CheckpointManager()
    with pytest.raises(RuntimeError):
        mgr.dump(job_id="j", pid=1)
    with pytest.raises(KeyError):  # unknown checkpoint: dump first
        mgr.persist("chk-x")
    with pytest.raises(RuntimeError):
        mgr.restore("chk-x")


def test_transfer_manager_fails_closed():
    mgr = TransferManager()
    with pytest.raises(RuntimeError):
        mgr.upload("j")
    with pytest.raises(RuntimeError):
        mgr.download("j")


def test_plan_store_step_tracking(tmp_path):
    from orchestrator.migration_planner import MigrationPlanner
    reg = _registry(tmp_path)
    store = PlanStore()
    planner = MigrationPlanner()
    plan = _plan()
    v2dag = planner.to_v2_dag(plan)
    plan.plan_id = "plan-track-1"
    from orchestrator.models_v2 import MigrationPlanV2, compute_plan_hash
    v2plan = MigrationPlanV2(
        plan_id="plan-track-1", job_id="job-1", migration_id=plan.migration_id,
        regime="ARBITRAGE", source_pool_id="s", target_pool_id="t",
        created_at="2026-01-01T00:00:00", expires_at="2099-01-01T00:00:00",
        steps=v2dag)
    v2plan.plan_hash = compute_plan_hash(v2plan)
    store.put_plan(v2plan)
    # Emulate coordinator tracking: PRECHECK running→succeeded across a hop.
    pre = next(s for s in v2dag if s.type == PlanStepType.PRECHECK)
    store.update_step_state("plan-track-1", pre.step_id, "PENDING", "RUNNING")
    store.update_step_state("plan-track-1", pre.step_id, "RUNNING", "SUCCEEDED")
    doc = store.get_plan("plan-track-1")
    assert next(s for s in doc["steps"] if s["type"] == "PRECHECK")["state"] == "SUCCEEDED"
