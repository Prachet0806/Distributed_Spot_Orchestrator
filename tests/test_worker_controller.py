"""Sprint 5 (Track D3): worker-controller trust, emulated (W1–W5, I8).

Vehicles: I8 (worker authority prohibition), W1–W5 ceremonies, T4
refusal → reconciliation finding, fail-closed controller fencing.
"""
import json
from datetime import datetime

import pytest

from worker.controller import (
    ControllerError,
    WorkerController,
)
from orchestrator import worker_controller as wctl
from orchestrator.migration_coordinator import MigrationCoordinator, MigrationState
from orchestrator.migration_planner import MigrationPlanner
from orchestrator.policy_engine import PolicyDecision, Decision, MigrationRegime
from orchestrator.checkpoint_manager import (
    CheckpointManager, CheckpointResult, RestoreResult)
from orchestrator.provisioner import Provisioner
from orchestrator.transfer_manager import TransferManager
from orchestrator.reconciliation_manager import (
    MismatchType, ReconciliationManager,
)
from orchestrator.worker_telemetry import (
    parse_heartbeat, parse_preflight_kv, parse_refusal, refusal_to_finding,
)
from orchestrator.workload_estimator import WorkloadEstimate
from storage.checkpoint_store import CheckpointStore
from storage.job_registry import JobRegistry


class _Exec:
    def __init__(self, dead=True):
        self.kills = []
        self.dead = dead
        self.preflights = 0

    def terminate(self, pid):
        self.kills.append(pid)

    def verify(self, pid):
        return self.dead

    def preflight(self):
        self.preflights += 1
        return "passed=true\ncriu_version=3.19\n"


def _cmd(op="op-1", epoch=0, instance="i-1", action="terminate", pid=4242):
    return {"instance_id": instance, "operation_id": op,
            "expected_execution_epoch": epoch, "action": action, "pid": pid}


# -- W1 lifecycle --
def test_boot_binds_identity_from_env(monkeypatch):
    monkeypatch.setenv("INSTANCE_ID", "i-xyz")
    monkeypatch.setenv("WORKLOAD_EXECUTION_EPOCH", "7")
    exe = _Exec()
    ctl = WorkerController(executor=exe)
    assert (ctl.instance_id, ctl.admission_epoch) == ("i-xyz", 7)
    hb = ctl.heartbeat(job_id="job-7")
    parsed = parse_heartbeat(hb)  # emission validates on intake
    assert parsed["instance_id"] == "i-xyz"
    assert ctl.preflight() == "passed=true\ncriu_version=3.19\n"
    assert exe.preflights == 1


def test_boot_refuses_missing_identity(monkeypatch):
    monkeypatch.delenv("INSTANCE_ID", raising=False)
    with pytest.raises(ControllerError, match="instance identity"):
        WorkerController(executor=_Exec())


# -- W2 binding --
def test_wrong_instance_refused_pairing():
    ctl = WorkerController(instance_id="i-1", admission_epoch=0,
                           executor=_Exec())
    out = ctl.handle_command(_cmd(instance="i-OTHER"))
    assert out["accepted"] is False and out["reason"] == "PAIRING_FAILED"
    refusal = parse_refusal(ctl.refusals[-1])  # T4 schema holds
    assert refusal["reason"] == "PAIRING_FAILED"


# -- W3 epoch gate --
@pytest.mark.parametrize("expected,accepted", [(0, True), (3, True), (-1, False)])
def test_epoch_gate_stale_refused_forward_accepted(expected, accepted):
    ctl = WorkerController(instance_id="i-1", admission_epoch=0,
                           executor=_Exec())
    out = ctl.handle_command(_cmd(epoch=expected))
    assert out["accepted"] is accepted
    if not accepted:
        assert out["reason"] == "EPOCH_MISMATCH"


def test_missing_epoch_refused():
    ctl = WorkerController(instance_id="i-1", admission_epoch=0,
                           executor=_Exec())
    out = ctl.handle_command({"instance_id": "i-1", "operation_id": "op-1",
                              "action": "terminate", "pid": 1})
    assert out["accepted"] is False


# -- W4 replay dedup --
def test_triple_execute_converges_without_reexec():
    exe = _Exec()
    ctl = WorkerController(instance_id="i-1", admission_epoch=0, executor=exe)
    outs = [ctl.handle_command(_cmd(op="op-same")) for _ in range(3)]
    assert all(o["accepted"] for o in outs)
    assert outs[0] == outs[1] == outs[2]
    assert exe.kills == [4242]  # executed once (I7 controller-side)


# -- I8 autonomy prohibition --
@pytest.mark.parametrize("action", ["migrate", "recover", "replan",
                                    "supersede", "fence", "checkpoint"])
def test_decision_shaped_commands_refused(action):
    ctl = WorkerController(instance_id="i-1", admission_epoch=0,
                           executor=_Exec())
    out = ctl.handle_command(_cmd(action=action))
    assert out["accepted"] is False
    assert out["reason"] == "UNKNOWN_OPERATION"


def test_no_migration_entrypoint_exists():
    ctl = WorkerController(instance_id="i-1", admission_epoch=0,
                           executor=_Exec())
    for name in ("migrate", "recover", "replan", "supersede",
                 "start_migration", "decide"):
        assert not hasattr(ctl, name)


# -- W5 refusal → finding --
def test_refusal_files_reconciliation_finding():
    ctl = WorkerController(instance_id="i-1", admission_epoch=5,
                           executor=_Exec())
    out = ctl.handle_command(_cmd(epoch=0))  # stale CPR
    assert out["accepted"] is False
    finding = refusal_to_finding(parse_refusal(ctl.refusals[-1]))
    assert finding["mismatch_type"] == "EPOCH_MISMATCH"

    class _Reg:
        def __init__(self):
            self.jobs = {"job-1": {"job_id": "job-1", "state": "RUNNING",
                                   "execution_epoch": 0}}

        def get(self, jid):
            return dict(self.jobs[jid])

        def transition(self, jid, to_state, **kw):
            self.jobs[jid]["state"] = to_state
            return dict(self.jobs[jid])

        def list_by_state(self, state):
            return [dict(j) for j in self.jobs.values()
                    if j.get("state") == state]

    recon = ReconciliationManager(_Reg(), None, None)
    findings = wctl.file_refusal(ctl.refusals[-1], recon, job_id="job-1",
                                 migration_id="m-1")
    assert len(findings) == 1
    assert findings[0].mismatch_type == MismatchType.EPOCH_MISMATCH


# -- CPR client hooks --
def _hooks(controller, sink, epoch=0):
    return wctl.make_controller_fence_hooks(
        lambda host: controller.handle_command,
        lambda plan: {"host": "10.0.0.9",
                      "pid": getattr(plan, "pid", None),
                      "instance_id": "i-1"},
        epoch_resolver=lambda plan: epoch,
        refusal_sink=sink)


def test_client_accepts_healthy_fence():
    exe = _Exec(dead=True)
    ctl = WorkerController(instance_id="i-1", admission_epoch=0, executor=exe)
    sunk = []
    hooks = _hooks(ctl, sunk.append)

    class _Plan:
        pid = 4242
        execution_epoch = 0

    hooks["terminate"](_Plan())
    assert exe.kills == [4242]
    assert hooks["verify"](_Plan()) is True
    assert sunk == []  # no refusals on the happy path


def test_client_fails_closed_on_stale_epoch():
    exe = _Exec(dead=True)
    ctl = WorkerController(instance_id="i-1", admission_epoch=5, executor=exe)
    sunk = []
    hooks = _hooks(ctl, sunk.append, epoch=0)

    class _Plan:
        pid = 4242
        execution_epoch = 0

    with pytest.raises(RuntimeError, match="refused.*EPOCH_MISMATCH"):
        hooks["terminate"](_Plan())
    assert exe.kills == []  # stale CPR fenced nothing
    assert len(sunk) == 1 and sunk[0]["reason"] == "EPOCH_MISMATCH"
    with pytest.raises(RuntimeError, match="refused"):
        hooks["verify"](_Plan())


def test_client_verify_alive_is_not_confirmed():
    exe = _Exec(dead=False)  # source still alive
    ctl = WorkerController(instance_id="i-1", admission_epoch=0, executor=exe)
    hooks = _hooks(ctl, lambda r: None)

    class _Plan:
        pid = 4242
        execution_epoch = 0

    hooks["terminate"](_Plan())
    assert hooks["verify"](_Plan()) is False


# -- coordinator integration: fenced through the controller --
def _est():
    return WorkloadEstimate(
        job_id="job-1", execution_epoch=0, progress=0.5,
        remaining_runtime_seconds=7200, expected_completion_at=None,
        prediction_confidence=0.8, checkpoint_size_estimate_bytes=100,
        checkpoint_duration_estimate_seconds=1.0,
        estimated_at=datetime.utcnow(), estimator_version="v",
        model_version="v", observation_snapshot_version="1")


def _dump(job_id, pid, host, timeout=300.0):
    return CheckpointResult(checkpoint_id=f"chk-{job_id}", size_bytes=64,
                            duration_seconds=0.01, digest="sha256:ctl",
                            status="LOCAL", job_id=job_id, lineage_id=job_id)


def _restore(checkpoint_id, host=None, timeout=300.0):
    return RestoreResult(checkpoint_id=checkpoint_id, success=True,
                         duration_seconds=0.01, process_id=4242)


class _Storage:
    bucket = "ctl-bkt"

    def upload(self, job_id, src=None):
        return f"{job_id}.tar.gz"

    def download(self, job_id, dst=None):
        return None


class _Validator:
    def validate(self, migration_id, job_id, epoch, **kw):
        return {"passed": True, "migration_id": migration_id,
                "job_id": job_id, "epoch": epoch}


class _Cleanup:
    def cleanup_migration(self, plan, operations, safety_critical_only=False):
        return []


def _run_with_controller(tmp_path, admission_epoch, prototype_epoch=0):
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({"job-1": {
        "job_id": "job-1", "state": "RUNNING", "region": "us-east-1",
        "public_ip": "10.0.0.9", "pid": 4242, "version": 0,
        "execution_epoch": 0, "active_migration_id": None}}))
    reg = JobRegistry(str(path))
    exe = _Exec(dead=True)
    ctl = WorkerController(instance_id="i-1", admission_epoch=admission_epoch,
                           executor=exe)
    sunk = []
    hooks = wctl.make_controller_fence_hooks(
        lambda host: ctl.handle_command,
        lambda plan: {"host": "10.0.0.9", "pid": plan.pid,
                      "instance_id": "i-1"},
        epoch_resolver=lambda plan: prototype_epoch,
        refusal_sink=sunk.append)
    coord = MigrationCoordinator(
        registry=reg, provisioner=Provisioner(),
        checkpoint_manager=CheckpointManager(
            storage=_Storage(), dump_handler=_dump, restore_handler=_restore),
        transfer_manager=TransferManager(storage=_Storage()),
        validator=_Validator(), cleanup_executor=_Cleanup(),
        fence_invalidate=hooks["invalidate"],
        fence_terminate=hooks["terminate"],
        fence_verify=hooks["verify"],
        snapshot_provider=lambda jid, phase: {"progress": 0.5},
        step_timeout_seconds=30.0)
    plan = MigrationPlanner().create_plan(
        job_id="job-1", execution_epoch=0, regime=MigrationRegime.ARBITRAGE,
        policy_decision=PolicyDecision(
            decision=Decision.MIGRATE, regime=MigrationRegime.ARBITRAGE,
            reason="t", target_candidate_id="pool-t", confidence=0.9,
            metadata={}),
        source_pool_id="pool-s", target_pool_id="pool-t",
        workload_estimate=_est())
    plan.pid = 4242
    plan.source_host = "10.0.0.9"
    return coord.execute_plan(plan), reg, exe, sunk


def test_coordinator_fences_through_controller(tmp_path):
    out, reg, exe, sunk = _run_with_controller(tmp_path, admission_epoch=0)
    assert out.current_state == MigrationState.SUCCESS
    assert exe.kills == [4242]  # fence actuated via the controller channel
    assert sunk == []
    assert reg.get("job-1")["execution_epoch"] == 1


def test_coordinator_stale_controller_fences_nothing_and_gates(tmp_path):
    out, reg, exe, sunk = _run_with_controller(tmp_path, admission_epoch=5)
    assert out.current_state == MigrationState.FAILED
    assert exe.kills == []  # stale CPR fenced nothing
    assert reg.get("job-1")["state"] == "RECONCILIATION_REQUIRED"
    assert len(sunk) >= 1  # refusals filed, never silent
