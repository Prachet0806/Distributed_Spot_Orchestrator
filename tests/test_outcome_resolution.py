"""Sprint 6 (Track C5/C4): outcome-resolution closure (S08/S22/S23, I4).

Every executor × UNKNOWN must resolve via probe or escalate without blind
retry. Throttle-class failures back off inside budgets and deadlines.
"""
import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from orchestrator.checkpoint_manager import (
    CheckpointManager, CheckpointResult, RestoreResult)
from orchestrator.migration_coordinator import MigrationCoordinator, MigrationState
from orchestrator.migration_planner import MigrationPlanner
from orchestrator.policy_engine import PolicyDecision, Decision, MigrationRegime
from orchestrator.provisioner import Provisioner
from orchestrator.step_retry import CodedError
from orchestrator.transfer_manager import TransferManager
from orchestrator.workload_estimator import WorkloadEstimate
from storage.job_registry import JobRegistry


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


def _registry(tmp_path):
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({"job-1": {
        "job_id": "job-1", "state": "RUNNING", "region": "us-east-1",
        "public_ip": "10.0.0.9", "pid": 4242, "version": 0,
        "execution_epoch": 0, "active_migration_id": None}}))
    return JobRegistry(str(path))


def _dump(job_id, pid, host, timeout=300.0):
    return CheckpointResult(checkpoint_id=f"chk-{job_id}", size_bytes=64,
                            duration_seconds=0.01, digest="sha256:res",
                            status="LOCAL", job_id=job_id, lineage_id=job_id)


def _restore(checkpoint_id, host=None, timeout=300.0):
    return RestoreResult(checkpoint_id=checkpoint_id, success=True,
                         duration_seconds=0.01, process_id=4242)


class _Storage:
    bucket = "res-bkt"

    def upload(self, job_id, src=None):
        return f"{job_id}.tar.gz"

    def download(self, job_id, dst=None):
        return None


class _Validator:
    def __init__(self, passed=True, slow=0.0):
        self._passed = passed
        self._slow = slow

    def validate(self, migration_id, job_id, epoch, **kw):
        if self._slow:
            time.sleep(self._slow)
        return {"passed": self._passed, "migration_id": migration_id,
                "job_id": job_id, "epoch": epoch}


class _Cleanup:
    def cleanup_migration(self, plan, operations, safety_critical_only=False):
        return []


class _ScriptedProvisioner:
    """Provisioner with scripted UNKNOWN→outcome status sequence."""

    def __init__(self, script, slow=0.0):
        self.script = list(script)
        self.slow = slow
        self.calls = []
        self.seen_ops = []

    def provision_with_operation(self, pool_id, operation_id=None, tags=None):
        self.calls.append(operation_id)
        self.seen_ops.append(operation_id)
        if self.slow:
            time.sleep(self.slow)
        from orchestrator.provisioner import ProvisionResult
        return ProvisionResult(instance_id="i-res", pool_id=pool_id)

    def get_operation_status(self, operation_id):
        if self.script:
            return {"state": self.script.pop(0)}
        return {"state": "UNKNOWN"}

    def terminate(self, instance_id):
        return True


def _plan(**kw):
    params = dict(job_id="job-1", execution_epoch=0,
                  regime=MigrationRegime.ARBITRAGE, policy_decision=_decision(),
                  source_pool_id="pool-s", target_pool_id="pool-t",
                  workload_estimate=_est())
    params.update(kw)
    plan = MigrationPlanner().create_plan(**params)
    plan.pid = 4242
    plan.source_host = "10.0.0.9"
    return plan


def _coordinator(reg, provisioner=None, dump=None, restore=None,
                 validator=None, transfer_storage=None, **kw):
    calls = []
    kw.setdefault("step_timeout_seconds", 30.0)
    return MigrationCoordinator(
        registry=reg,
        provisioner=provisioner or Provisioner(),
        checkpoint_manager=CheckpointManager(
            storage=_Storage(), dump_handler=dump or _dump,
            restore_handler=restore or _restore),
        transfer_manager=TransferManager(storage=transfer_storage or _Storage()),
        validator=validator or _Validator(),
        cleanup_executor=_Cleanup(),
        fence_invalidate=lambda p: calls.append("invalidate"),
        fence_terminate=lambda p: calls.append("terminate"),
        fence_verify=lambda p: calls.append("verify") or True,
        snapshot_provider=lambda jid, phase: {"progress": 0.5},
        **kw), calls


def _unknown_ops(out):
    return [o for o in out.operations if o.status == "UNKNOWN"]


# -- S08: provision UNKNOWN resolves without blind retry --
def test_s08_provision_unknown_resolves_succeeded(tmp_path):
    reg = _registry(tmp_path)
    prov = _ScriptedProvisioner(["UNKNOWN", "UNKNOWN", "SUCCEEDED"], slow=0.5)
    coord, fence_calls = _coordinator(
        reg, provisioner=prov, step_timeout_seconds=0.05,
        unknown_resolution_budget_seconds=5.0,
        unknown_poll_interval_seconds=0.01)
    out = coord.execute_plan(_plan())
    assert out.current_state == MigrationState.SUCCESS
    assert fence_calls == ["invalidate", "terminate", "verify"]
    assert prov.calls and len(set(prov.seen_ops)) == 1  # one logical op
    assert len(prov.calls) == 1  # never blind-retried
    assert _unknown_ops(out) == []
    assert reg.get("job-1")["execution_epoch"] == 1


def test_s08_provision_unknown_unresolved_escalates(tmp_path):
    reg = _registry(tmp_path)
    prov = _ScriptedProvisioner(["UNKNOWN"] * 50, slow=0.5)
    coord, fence_calls = _coordinator(
        reg, provisioner=prov, step_timeout_seconds=0.05,
        unknown_resolution_budget_seconds=0.05,
        unknown_poll_interval_seconds=0.01)
    out = coord.execute_plan(_plan())
    assert out.current_state == MigrationState.FAILED
    assert fence_calls == []  # fenced nothing
    unknowns = _unknown_ops(out)
    assert len(unknowns) == 1 and unknowns[0].step_name == "provisioning"
    assert len(prov.calls) == 1  # single attempt, then escalate
    job = reg.get("job-1")
    assert job["state"] == "RUNNING"  # pre-fence, no durable checkpoint
    assert job["execution_epoch"] == 0


# -- S23: UNKNOWN at every executor surfaces (never silent, never retried) --
def _slow_never_returns(*_a, **_k):
    # Longer than any step timeout below, short enough that executor
    # shutdown (which joins pending futures) stays cheap.
    time.sleep(2.0)
    raise AssertionError("unreachable")


def test_s23_checkpoint_timeout_without_probe_is_unknown(tmp_path):
    reg = _registry(tmp_path)
    coord, _ = _coordinator(reg, dump=_slow_never_returns,
                            step_timeout_seconds=0.05)
    out = coord.execute_plan(_plan())
    assert out.current_state == MigrationState.FAILED
    unknowns = _unknown_ops(out)
    assert len(unknowns) == 1 and unknowns[0].step_name == "checkpointing"


def test_s23_transfer_unknown_with_dead_probe(tmp_path):
    reg = _registry(tmp_path)

    class _DeadStorage(_Storage):
        def download(self, job_id, dst=None):
            time.sleep(2.0)
            raise AssertionError("unreachable")

    coord, fence_calls = _coordinator(
        reg, transfer_storage=_DeadStorage(), step_timeout_seconds=0.05,
        unknown_resolution_budget_seconds=0.05,
        unknown_poll_interval_seconds=0.01)
    out = coord.execute_plan(_plan())
    assert out.current_state == MigrationState.FAILED
    assert fence_calls == []
    unknowns = _unknown_ops(out)
    assert len(unknowns) == 1 and unknowns[0].step_name == "transferring"


def test_s23_restore_unknown_with_dead_probe(tmp_path):
    reg = _registry(tmp_path)

    def _slow_restore(checkpoint_id, host=None, timeout=300.0):
        time.sleep(2.0)
        raise AssertionError("unreachable")

    coord, fence_calls = _coordinator(
        reg, restore=_slow_restore, step_timeout_seconds=0.05,
        unknown_resolution_budget_seconds=0.05,
        unknown_poll_interval_seconds=0.01)
    out = coord.execute_plan(_plan())
    assert out.current_state == MigrationState.FAILED
    assert fence_calls == []
    unknowns = _unknown_ops(out)
    assert len(unknowns) == 1 and unknowns[0].step_name == "restoring"


def test_s23_validate_timeout_without_probe_is_unknown_post_fence(tmp_path):
    reg = _registry(tmp_path)
    coord, fence_calls = _coordinator(
        reg, validator=_Validator(slow=2.0), step_timeout_seconds=0.05)
    out = coord.execute_plan(_plan())
    assert out.current_state == MigrationState.FAILED
    assert fence_calls == ["invalidate", "terminate", "verify"]
    unknowns = _unknown_ops(out)
    assert len(unknowns) == 1 and unknowns[0].step_name == "validating"
    job = reg.get("job-1")
    assert job["state"] == "RECOVERY_REQUIRED"  # forward-only post-fence
    assert job["execution_epoch"] == 1


# -- S22: throttle-class failures back off inside budgets and deadlines --
def test_s22_capacity_unavailable_retries_with_backoff_then_fails(tmp_path):
    reg = _registry(tmp_path)
    sleeps, calls = [], []

    def _throttled(pool_id, operation_id=None, tags=None):
        calls.append(1)
        raise CodedError("CAPACITY_UNAVAILABLE", "throttled",
                         transience="TRANSIENT")

    class _ThrottleProv(Provisioner):
        def provision_with_operation(self, pool_id, operation_id=None,
                                     tags=None):
            return _throttled(pool_id, operation_id, tags)

    coord, _ = _coordinator(reg, provisioner=_ThrottleProv(),
                            sleep_fn=sleeps.append)
    out = coord.execute_plan(_plan())
    assert out.current_state == MigrationState.FAILED
    assert len(calls) == 3  # ADR-012 default budget
    assert len(sleeps) == 2  # backoff between attempts only
    assert _unknown_ops(out) == []  # coded failure, never UNKNOWN


def test_s22_backoff_yielding_to_emergency_deadline(tmp_path):
    # Direct retry-path proof (the precheck budget gate would refuse a
    # 1 s-deadline plan before any attempt; here the deadline collapses
    # AFTER the first attempt fails, so backoff itself must yield).
    from types import SimpleNamespace

    from orchestrator.migration_coordinator import (
        DeadlineExceeded, MigrationExecutionState,
    )
    sleeps, calls = [], []

    def _throttled():
        calls.append(1)
        raise CodedError("CAPACITY_UNAVAILABLE", "throttled",
                         transience="TRANSIENT")

    plan = _plan(
        regime=MigrationRegime.EMERGENCY,
        policy_decision=PolicyDecision(
            decision=Decision.MIGRATE, regime=MigrationRegime.EMERGENCY,
            reason="t", target_candidate_id="pool-t", confidence=0.9,
            metadata={}),
        absolute_deadline=datetime.now(timezone.utc) + timedelta(seconds=600))
    coord = MigrationCoordinator(
        registry=SimpleNamespace(get=lambda j: None,
                                 transition=lambda *a, **_k: None),
        provisioner=SimpleNamespace(), checkpoint_manager=SimpleNamespace(),
        transfer_manager=SimpleNamespace(), validator=SimpleNamespace(),
        cleanup_executor=SimpleNamespace(), sleep_fn=sleeps.append,
        step_timeout_seconds=30.0)
    coord._execution_state = MigrationExecutionState(
        plan=plan, current_state=MigrationState.PROVISIONING,
        state_entered_at=datetime.utcnow(), execution_epoch=0,
        expected_epoch=0)
    coord._start_mono = coord._monotonic()
    # Collapse the deadline after the plan admitted: backoff (~2 s) now
    # exceeds the remaining budget (~1 s) → yield, no doomed sleep.
    plan.absolute_deadline = datetime.now(timezone.utc) + timedelta(seconds=1)
    with pytest.raises(DeadlineExceeded):
        coord._run_operation_with_retry(
            step_type="PROVISION", operation_id="op-1", func=_throttled,
            timeout=30.0, default_code="PROVISION_FAILED")
    assert len(calls) == 1 and sleeps == []
