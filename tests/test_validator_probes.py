"""Sprint 2 (Track C2): transport-backed L1/L2 validator probes.

Vehicles: C-VALID (L1-L4 verdicts), S19 (INCONCLUSIVE → Recovery Policy,
never silent promote), I7/I15 (no stub-pass, no fabricated health).
"""
import json
from datetime import datetime, timezone, timedelta

from orchestrator import validator_probes as probes
from orchestrator.migration_coordinator import MigrationCoordinator
from orchestrator.migration_planner import MigrationPlanner, MigrationState
from orchestrator.policy_engine import PolicyDecision, Decision, MigrationRegime
from orchestrator.checkpoint_manager import (
    CheckpointManager, CheckpointResult, RestoreResult)
from orchestrator.provisioner import Provisioner
from orchestrator.transfer_manager import TransferManager
from orchestrator.validator import Validator, ValidationResult
from orchestrator.workload_estimator import WorkloadEstimate
from storage.checkpoint_store import CheckpointStore
from storage.job_registry import JobRegistry

NOW = datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc)


class _StubTransport:
    """Dict-returning transport; records commands, never touches the net."""

    def __init__(self, files=None, rc=0, err=None):
        self.files = files or {}
        self.rc = rc
        self.err = err
        self.commands = []

    def run_command(self, command, timeout=60.0):
        self.commands.append(command)
        if self.err is not None:
            raise self.err
        for name, text in self.files.items():
            if name in command:
                return {"returncode": 0, "stdout": text, "stderr": ""}
        if command.startswith("ps -p"):
            return {"returncode": self.rc, "stdout": "", "stderr": ""}
        return {"returncode": 1, "stdout": "", "stderr": "missing"}

    def close(self):
        return None


def _hb(age_seconds=10):
    return json.dumps({
        "schema": "heartbeat/v1", "job_id": "job-1", "instance_id": "i-1",
        "execution_epoch": 1, "pid": 4242,
        "observed_at": (NOW - timedelta(seconds=age_seconds)).isoformat(),
        "workload": {"state": "RUNNING"},
    })


def _ctx(**kw):
    doc = {"migration_id": "m-1", "job_id": "job-1",
           "target_host": "10.0.0.9", "target_pid": 4242}
    doc.update(kw)
    return doc


def _factory(transport):
    return lambda host: transport


# -- L1 --
def test_l1_passes_when_target_process_runs():
    t = _StubTransport(rc=0)
    probe = probes.make_infra_probe(
        _factory(t), probes.make_context_provider({"m-1": _ctx()}))
    details = probe(migration_id="m-1")
    assert details["passed"] is True
    assert details["process_running"] is True
    assert details["probe"] == "transport-v1"
    assert any("ps -p 4242" in c for c in t.commands)


def test_l1_fails_when_target_process_gone():
    t = _StubTransport(rc=1)
    probe = probes.make_infra_probe(
        _factory(t), probes.make_context_provider({"m-1": _ctx()}))
    details = probe(migration_id="m-1")
    assert details["passed"] is False
    assert details["process_running"] is False
    assert "inconclusive" not in details  # evidence of unhealth ⇒ INVALID


def test_l1_fails_closed_on_transport_error():
    t = _StubTransport(err=RuntimeError("ssh down"))
    probe = probes.make_infra_probe(
        _factory(t), probes.make_context_provider({"m-1": _ctx()}))
    details = probe(migration_id="m-1")
    assert details["passed"] is False
    assert details["target_reachable"] is False
    assert details["error"] == "ssh down"


def test_l1_inconclusive_without_context_or_host():
    probe = probes.make_infra_probe(_factory(_StubTransport()),
                                    probes.make_context_provider({}))
    for mid in (None, "m-missing"):
        details = probe(migration_id=mid)
        assert details["passed"] is False
        assert details["inconclusive"] is True
    no_host = probes.make_infra_probe(
        _factory(_StubTransport()),
        probes.make_context_provider({"m-1": _ctx(target_host=None)}))
    assert no_host(migration_id="m-1")["inconclusive"] is True
    no_pid = probes.make_infra_probe(
        _factory(_StubTransport()),
        probes.make_context_provider({"m-1": _ctx(target_pid=None)}))
    assert no_pid(migration_id="m-1")["inconclusive"] is True


# -- L2 --
def test_l2_passes_on_fresh_heartbeat():
    t = _StubTransport(files={"heartbeat.json": _hb()})
    probe = probes.make_app_probe(
        _factory(t), probes.make_context_provider({"m-1": _ctx()}),
        clock=lambda: NOW)
    details = probe(migration_id="m-1")
    assert details["passed"] is True
    assert details["workload_responding"] is True


def test_l2_inconclusive_on_stale_or_missing_heartbeat():
    stale = _StubTransport(files={"heartbeat.json": _hb(age_seconds=10_000)})
    probe = probes.make_app_probe(
        _factory(stale), probes.make_context_provider({"m-1": _ctx()}),
        clock=lambda: NOW)
    details = probe(migration_id="m-1")
    assert details["passed"] is False
    assert details["inconclusive"] is True  # S19: never silent promote

    missing = probes.make_app_probe(
        _factory(_StubTransport()),
        probes.make_context_provider({"m-1": _ctx()}), clock=lambda: NOW)
    assert missing(migration_id="m-1")["inconclusive"] is True


# -- Validator verdict mapping --
def _snaps():
    snap = {"progress": 0.5, "cpu_utilization_delta": 0.5,
            "memory_utilization_delta": 0.4, "progress_delta": 0.5}
    return snap, dict(snap)


def test_healthy_probes_validate():
    store = {"m-1": _ctx()}
    ctx = probes.make_context_provider(store)
    t = _StubTransport(files={"heartbeat.json": _hb()}, rc=0)
    v = Validator(infra_probe=probes.make_infra_probe(lambda h: t, ctx),
                  app_probe=probes.make_app_probe(lambda h: t, ctx,
                                                  clock=lambda: NOW))
    src, tgt = _snaps()
    rep = v.validate("m-1", "job-1", 1, src, tgt)
    assert rep.overall_result == ValidationResult.PASSED
    assert rep.to_v2_verdict() == ValidationResult.VALID.value
    assert "stub-v1" not in json.dumps([c.details for c in rep.checks])


def test_stale_l2_is_inconclusive_not_invalid():
    store = {"m-1": _ctx()}
    ctx = probes.make_context_provider(store)
    t = _StubTransport(files={"heartbeat.json": _hb(age_seconds=10_000)}, rc=0)
    v = Validator(infra_probe=probes.make_infra_probe(lambda h: t, ctx),
                  app_probe=probes.make_app_probe(lambda h: t, ctx,
                                                  clock=lambda: NOW))
    src, tgt = _snaps()
    rep = v.validate("m-1", "job-1", 1, src, tgt)
    assert rep.overall_result == ValidationResult.PARTIAL
    assert rep.to_v2_verdict() == ValidationResult.INCONCLUSIVE.value


def test_dead_l1_is_invalid():
    store = {"m-1": _ctx()}
    ctx = probes.make_context_provider(store)
    t = _StubTransport(files={"heartbeat.json": _hb()}, rc=1)
    v = Validator(infra_probe=probes.make_infra_probe(lambda h: t, ctx),
                  app_probe=probes.make_app_probe(lambda h: t, ctx,
                                                  clock=lambda: NOW))
    src, tgt = _snaps()
    rep = v.validate("m-1", "job-1", 1, src, tgt)
    assert rep.overall_result == ValidationResult.FAILED
    assert rep.to_v2_verdict() == ValidationResult.INVALID.value


# -- Coordinator: sink publication + real-validator execution --
def _est():
    return WorkloadEstimate(
        job_id="job-1", execution_epoch=0, progress=0.5,
        remaining_runtime_seconds=7200, expected_completion_at=None,
        prediction_confidence=0.8, checkpoint_size_estimate_bytes=100,
        checkpoint_duration_estimate_seconds=1.0,
        estimated_at=datetime.utcnow(), estimator_version="v",
        model_version="v", observation_snapshot_version="1")


def _decision():
    return PolicyDecision(decision=Decision.MIGRATE, regime=MigrationRegime.ARBITRAGE,
                          reason="t", target_candidate_id="pool-t",
                          confidence=0.9, metadata={})


def _snap_fn(job_id, phase):
    return {"progress": 0.5, "cpu_utilization_delta": 0.5,
            "memory_utilization_delta": 0.5, "progress_delta": 0.5}


def _dump(job_id, pid, host, timeout=300.0):
    return CheckpointResult(checkpoint_id=f"chk-{job_id}", size_bytes=64,
                            duration_seconds=0.01, digest="sha256:probe",
                            status="LOCAL", job_id=job_id, lineage_id=job_id)


def _restore(checkpoint_id, host=None, timeout=300.0):
    return RestoreResult(checkpoint_id=checkpoint_id, success=True,
                         duration_seconds=0.01, process_id=4242)


class _Storage:
    bucket = "probe-bkt"

    def upload(self, job_id, src=None):
        return f"{job_id}.tar.gz"

    def download(self, job_id, dst=None):
        return None


class _Cleanup:
    def cleanup_migration(self, plan, operations, safety_critical_only=False):
        return ["partial_artifacts"]


def _coordinator(reg, sink, validator, store):
    calls = []
    return MigrationCoordinator(
        registry=reg, provisioner=Provisioner(),
        checkpoint_manager=CheckpointManager(
            storage=_Storage(), dump_handler=_dump, restore_handler=_restore,
            checkpoint_store=store),
        transfer_manager=TransferManager(storage=_Storage()),
        validator=validator, cleanup_executor=_Cleanup(),
        plan_store=None, checkpoint_store=store,
        fence_invalidate=lambda p: calls.append("invalidate"),
        fence_terminate=lambda p: calls.append("terminate"),
        fence_verify=lambda p: calls.append("verify") or True,
        snapshot_provider=_snap_fn,
        execution_context_sink=sink,
        step_timeout_seconds=30.0), calls


def test_coordinator_publishes_context_and_validates_with_probes(tmp_path):
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({"job-1": {
        "job_id": "job-1", "state": "RUNNING", "region": "us-east-1",
        "public_ip": "10.0.0.9", "pid": 4242, "version": 0,
        "execution_epoch": 0, "active_migration_id": None}}))
    reg = JobRegistry(str(path))
    published = []
    store = CheckpointStore()
    transport = _StubTransport(files={"heartbeat.json": _hb()}, rc=0)
    # Sink feeds the live provider map (same shape as main.py wiring).
    live = {}
    provider = probes.make_context_provider(live)
    validator = Validator(
        infra_probe=probes.make_infra_probe(lambda h: transport, provider),
        app_probe=probes.make_app_probe(lambda h: transport, provider,
                                        clock=lambda: NOW))
    coord, fence_calls = _coordinator(
        reg, lambda c: (published.append(c), live.__setitem__(
            c["migration_id"], c)), validator, store)
    plan = MigrationPlanner().create_plan(
        job_id="job-1", execution_epoch=0, regime=MigrationRegime.ARBITRAGE,
        policy_decision=_decision(), source_pool_id="pool-s",
        target_pool_id="pool-t", workload_estimate=_est())
    plan.pid = 4242
    plan.source_host = "10.0.0.9"
    out = coord.execute_plan(plan)
    assert out.current_state == MigrationState.SUCCESS
    assert out.target_pid == 4242  # captured from the restore result
    assert len(published) == 1
    assert published[0]["target_pid"] == 4242
    assert published[0]["migration_id"] == plan.migration_id
    assert fence_calls == ["invalidate", "terminate", "verify"]
    assert any("ps -p 4242" in c for c in transport.commands)
    assert reg.get("job-1")["execution_epoch"] == 1


def test_postfence_invalid_with_real_validator_is_forward_only(tmp_path):
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({"job-1": {
        "job_id": "job-1", "state": "RUNNING", "region": "us-east-1",
        "public_ip": "10.0.0.9", "pid": 4242, "version": 0,
        "execution_epoch": 0, "active_migration_id": None}}))
    reg = JobRegistry(str(path))
    live = {}
    provider = probes.make_context_provider(live)
    transport = _StubTransport(files={"heartbeat.json": _hb()}, rc=0)
    validator = Validator(
        infra_probe=probes.make_infra_probe(lambda h: transport, provider),
        app_probe=probes.make_app_probe(lambda h: transport, provider,
                                        clock=lambda: NOW))
    coord, _ = _coordinator(
        reg, lambda c: live.__setitem__(c["migration_id"], c),
        validator, CheckpointStore())

    def _drifted(job_id, phase):
        base = _snap_fn(job_id, phase)
        if phase == "post":
            base = dict(base, cpu_utilization_delta=5.0)  # tolerance breach
        return base

    coord._snapshot_provider = _drifted
    plan = MigrationPlanner().create_plan(
        job_id="job-1", execution_epoch=0, regime=MigrationRegime.ARBITRAGE,
        policy_decision=_decision(), source_pool_id="pool-s",
        target_pool_id="pool-t", workload_estimate=_est())
    plan.pid = 4242
    plan.source_host = "10.0.0.9"
    out = coord.execute_plan(plan)
    assert out.current_state == MigrationState.FAILED
    job = reg.get("job-1")
    assert job["state"] == "RECOVERY_REQUIRED"  # forward-only, epoch stands
    assert job["execution_epoch"] == 1


def test_inconclusive_with_real_validator_never_silently_promotes(tmp_path):
    """S19 remainder: evidence-absence → INCONCLUSIVE → FAILED, never SUCCESS.

    No execution-context sink is wired, so both probes report inconclusive;
    the real Validator verdict is INCONCLUSIVE and the coordinator fails
    the migration (post-fence: forward recovery) instead of promoting it.
    """
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({"job-1": {
        "job_id": "job-1", "state": "RUNNING", "region": "us-east-1",
        "public_ip": "10.0.0.9", "pid": 4242, "version": 0,
        "execution_epoch": 0, "active_migration_id": None}}))
    reg = JobRegistry(str(path))
    provider = probes.make_context_provider({})  # nothing published, ever
    transport = _StubTransport(files={"heartbeat.json": _hb()}, rc=0)
    validator = Validator(
        infra_probe=probes.make_infra_probe(lambda h: transport, provider),
        app_probe=probes.make_app_probe(lambda h: transport, provider,
                                        clock=lambda: NOW))
    # Pre-check the verdict itself is inconclusive-family, not VALID or INVALID.
    rep = validator.validate("m-9", "job-1", 1, _snap_fn("job-1", "pre"),
                             _snap_fn("job-1", "post"))
    assert rep.to_v2_verdict() == "INCONCLUSIVE"
    coord, _ = _coordinator(reg, None, validator, CheckpointStore())
    plan = MigrationPlanner().create_plan(
        job_id="job-1", execution_epoch=0, regime=MigrationRegime.ARBITRAGE,
        policy_decision=_decision(), source_pool_id="pool-s",
        target_pool_id="pool-t", workload_estimate=_est())
    plan.pid = 4242
    plan.source_host = "10.0.0.9"
    out = coord.execute_plan(plan)
    assert out.current_state == MigrationState.FAILED
    job = reg.get("job-1")
    assert job["state"] == "RECOVERY_REQUIRED"  # fenced: forward-only
    assert job["execution_epoch"] == 1  # ownership stands; nothing promoted
