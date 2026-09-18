"""Track B1/I14: Dynamo-backed stores wired; audit-before-irreversible.

Vehicles: I14 (audit-before-irreversible), C-E2E-adjacent terminal audit,
mandatory FENCE_STARTED/FENCE_CONFIRMED/MIGRATION_* events.
"""
import json
from datetime import datetime

from orchestrator.migration_planner import MigrationPlanner, MigrationState
from orchestrator.migration_coordinator import MigrationCoordinator
from orchestrator.policy_engine import PolicyDecision, Decision, MigrationRegime
from orchestrator.workload_estimator import WorkloadEstimate
from orchestrator.checkpoint_manager import (
    CheckpointManager, CheckpointResult, RestoreResult)
from orchestrator.provisioner import Provisioner
from orchestrator.transfer_manager import TransferManager
from storage.audit_store import AuditStore
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
                            duration_seconds=0.01, digest="sha256:audit",
                            status="LOCAL", job_id=job_id, lineage_id=job_id)


def _restore(checkpoint_id, host=None, timeout=300.0):
    return RestoreResult(checkpoint_id=checkpoint_id, success=True,
                         duration_seconds=0.01, process_id=4242)


class _Storage:
    bucket = "audit-bkt"

    def upload(self, job_id, src=None):
        return f"{job_id}.tar.gz"

    def download(self, job_id, dst=None):
        return None


class _Validator:
    def __init__(self, passed=True):
        self._passed = passed

    def validate(self, migration_id, job_id, epoch, **kw):
        return {"passed": self._passed, "migration_id": migration_id,
                "job_id": job_id, "epoch": epoch}


class _Cleanup:
    def cleanup_migration(self, plan, operations, safety_critical_only=False):
        return ["partial_artifacts"]


def _coordinator(reg, audit_store, validator=None):
    calls = []
    return MigrationCoordinator(
        registry=reg,
        provisioner=Provisioner(),
        checkpoint_manager=CheckpointManager(
            storage=_Storage(), dump_handler=_dump, restore_handler=_restore),
        transfer_manager=TransferManager(storage=_Storage()),
        validator=validator or _Validator(),
        cleanup_executor=_Cleanup(),
        plan_store=None, checkpoint_store=None, audit_store=audit_store,
        fence_invalidate=lambda plan: calls.append("invalidate"),
        fence_terminate=lambda plan: calls.append("terminate"),
        fence_verify=lambda plan: calls.append("verify") or True,
        step_timeout_seconds=30.0), calls


def _plan():
    return MigrationPlanner().create_plan(
        job_id="job-1", execution_epoch=0, regime=MigrationRegime.ARBITRAGE,
        policy_decision=_decision(), source_pool_id="pool-s",
        target_pool_id="pool-t", workload_estimate=_est())


def test_fence_audited_before_hooks_and_completed_on_success(tmp_path):
    reg = _registry(tmp_path)
    audit = AuditStore()
    coord, fence_calls = _coordinator(reg, audit)
    out = coord.execute_plan(_plan())
    assert out.current_state == MigrationState.SUCCESS
    types = [r["event_type"] for r in audit.list_by_job("job-1")]
    assert types == ["FENCE_STARTED", "FENCE_CONFIRMED", "MIGRATION_COMPLETED"]
    # Correlation envelope on every record.
    for record in audit.list_by_job("job-1"):
        assert record["migration_id"] == out.plan.migration_id
        assert record["job_id"] == "job-1"
        assert record["audit_id"] and record["plan_hash"] == out.plan.plan_hash
    assert fence_calls == ["invalidate", "terminate", "verify"]


def test_failed_audit_write_blocks_fencing(tmp_path):
    reg = _registry(tmp_path)

    class _BrokenAudit:
        def append(self, record):
            raise RuntimeError("audit store unavailable")

    coord, fence_calls = _coordinator(reg, _BrokenAudit())
    out = coord.execute_plan(_plan())
    # I14 fail-closed: no fence hook ran, migration did not succeed.
    assert fence_calls == []
    assert out.current_state == MigrationState.FAILED


def test_terminal_failure_is_audited(tmp_path):
    reg = _registry(tmp_path)
    audit = AuditStore()
    coord, _ = _coordinator(reg, audit, validator=_Validator(passed=False))
    out = coord.execute_plan(_plan())
    assert out.current_state == MigrationState.FAILED
    types = [r["event_type"] for r in audit.list_by_job("job-1")]
    assert "FENCE_STARTED" in types  # fencing ran (post-fence INVALID)
    assert "FENCE_CONFIRMED" in types
    assert types[-1] == "MIGRATION_FAILED"
    assert audit.list_by_job("job-1")[-1]["reason"]


def test_no_audit_store_preserves_legacy_behavior(tmp_path):
    reg = _registry(tmp_path)
    coord, _ = _coordinator(reg, None)
    out = coord.execute_plan(_plan())
    assert out.current_state == MigrationState.SUCCESS
