"""Phase 6c: emulated V2 end-to-end — observe→decide→plan→execute→audit."""
import json
from datetime import datetime

from orchestrator.workload_estimator import (
    WorkloadEstimator, WorkloadObservation, EstimatorConfig,
)
from orchestrator.workload_requirements import WorkloadRequirements, GPURequirement
from orchestrator.candidate_pool import (
    CandidatePool, PoolRuntimeProfile, PoolCapacityProfile,
)
from orchestrator.candidate_compatibility import CompatibilityEngine
from orchestrator.candidate_readiness import (
    ReadinessEngine, IAMReadiness, NetworkReadiness, StorageReadiness,
    CapacityEvidence, CapacityStatus, EvidenceSource,
)
from orchestrator.cost_risk_evaluator import CostRiskEvaluator
from orchestrator.policy_engine import (
    PolicyEngine, ArbitragePolicyConfig, MigrationRegime,
)
from orchestrator.migration_planner import MigrationPlanner, MigrationState
from orchestrator.migration_coordinator import MigrationCoordinator
from orchestrator.checkpoint_manager import CheckpointManager, CheckpointResult, RestoreResult
from orchestrator.provisioner import Provisioner
from orchestrator.transfer_manager import TransferManager
from orchestrator.validator import Validator
from orchestrator.reconciliation_manager import ReconciliationManager, ReconciliationTrigger
from orchestrator.event_ledger import EventLedger
from storage.job_registry import JobRegistry
from storage.plan_store import PlanStore
from storage.checkpoint_store import CheckpointStore
from storage.history_store import HistoryStore


def _dump(job_id, pid, host, timeout=300.0):
    return CheckpointResult(checkpoint_id=f"chk-{job_id}", size_bytes=64,
                            duration_seconds=0.01, digest="sha256:e2e",
                            status="LOCAL", job_id=job_id, lineage_id=job_id)


def _restore(checkpoint_id, host=None, timeout=300.0):
    return RestoreResult(checkpoint_id=checkpoint_id, success=True,
                         duration_seconds=0.01, process_id=4242)


class _Storage:
    def __init__(self):
        self.bucket = "e2e-bucket"

    def upload(self, job_id, src=None):
        return f"{job_id}.tar.gz"

    def download(self, job_id, dst=None):
        return None


class _Cleanup:
    def cleanup_migration(self, plan, operations, safety_critical_only=False):
        return ["partial_artifacts"]


def test_emulated_arbitrage_migration_end_to_end(tmp_path):
    # -- registry: eligible long-running job --
    reg_path = tmp_path / "registry.json"
    reg_path.write_text(json.dumps({"job-1": {
        "job_id": "job-1", "state": "RUNNING", "region": "us-east-1",
        "public_ip": "10.0.0.9", "pid": 4242, "version": 0,
        "execution_epoch": 0, "active_migration_id": None,
        "workload_type": "long", "progress": 0.5,
        "cpu_utilization": 0.5, "memory_utilization": 0.5}}))
    registry = JobRegistry(str(reg_path))
    job = registry.get("job-1")

    # -- observe / estimate (real) --
    estimator = WorkloadEstimator(EstimatorConfig())
    estimate = estimator.estimate(
        "job-1", 0, "long",
        WorkloadObservation(job_id="job-1", execution_epoch=0, progress=0.5,
                            checkpoint_size_bytes=1024,
                            checkpoint_duration_seconds=1.0,
                            cpu_utilization=0.5, memory_utilization=0.5,
                            observed_at=datetime.utcnow(),
                            observation_confidence=0.9))
    assert (estimate.remaining_runtime_seconds or 0) > 0

    # -- compatibility / readiness (real engines) --
    requirements = WorkloadRequirements(
        cpu_architecture="x86_64", min_cpu=1, min_memory_mb=1024,
        gpu=GPURequirement(required=False), reconnectable=True)
    pool = CandidatePool(
        pool_id="pool-us-west-2", provider="aws", account_id="1",
        region="us-west-2", availability_zone="us-west-2a",
        instance_type="t3.micro", architecture="x86_64",
        runtime_profile=PoolRuntimeProfile(artifact_digest="sha256:abc123",
                                           architecture="x86_64"),
        capacity_profile=PoolCapacityProfile(max_instances=10))
    compat = CompatibilityEngine().assess(requirements, pool)
    ready = ReadinessEngine().assess(
        requirements, pool,
        iam_ready=IAMReadiness(True, True, True, True),
        network_ready=NetworkReadiness(True, True, True, True),
        storage_ready=StorageReadiness(True, True, True),
        capacity_evidence=CapacityEvidence(
            status=CapacityStatus.AVAILABLE,
            source=EvidenceSource.HISTORICAL_PROVISIONING,
            observed_at=datetime.utcnow(), confidence=0.9,
            provisioning_success_rate=0.9, sample_count=10),
        artifact_available=True)
    assert compat.status.value == "COMPATIBLE"
    assert ready.status.value == "READY"

    # -- economics / policy (real) --
    evaluator = CostRiskEvaluator()
    stay = evaluator.evaluate_stay(0.10, estimate, 0.05)
    candidate = evaluator.evaluate_candidate(
        0.10, 0.02, estimate, 0.05, checkpoint_size_bytes=1024,
        checkpoint_duration_seconds=1.0)
    candidate.target_pool_id = "pool-us-west-2"
    policy = PolicyEngine(ArbitragePolicyConfig())
    decision = policy.decide(
        MigrationRegime.ARBITRAGE, stay, [candidate], estimate,
        [compat], [ready], job_context={"workload_type": "long"},
        favorable_observations=3, cooldown_remaining_seconds=0.0)
    assert decision.decision.value == "MIGRATE"
    assert decision.target_candidate_id == "pool-us-west-2"

    # -- plan (real, hash-pinned) --
    planner = MigrationPlanner()
    plan = planner.create_plan(
        job_id="job-1", execution_epoch=0, regime=MigrationRegime.ARBITRAGE,
        policy_decision=decision, source_pool_id="pool-us-east-1",
        target_pool_id="pool-us-west-2", workload_estimate=estimate,
        stay_analysis=stay, candidate_analysis=candidate)
    plan.pid = job["pid"]
    plan.source_host = job["public_ip"]
    assert plan.plan_hash and plan.fencing_authorized

    # -- durable audit trail: ledger + history (estimated vs actual split) --
    ledger = EventLedger()
    assert ledger.record(plan.migration_id, "coordinator") is True
    assert ledger.record(plan.migration_id, "coordinator") is False
    history = HistoryStore()
    history.insert({"migration_id": plan.migration_id, "job_id": "job-1",
                    "regime": "ARBITRAGE", "estimated_cost": 1.0})

    # -- execute (real coordinator/validator/stores, emulated transport) --
    plan_store, checkpoint_store = PlanStore(), CheckpointStore()
    fence_calls = []
    coord = MigrationCoordinator(
        registry=registry, provisioner=Provisioner(),
        checkpoint_manager=CheckpointManager(
            storage=_Storage(), dump_handler=_dump, restore_handler=_restore,
            checkpoint_store=checkpoint_store),
        transfer_manager=TransferManager(storage=_Storage()),
        validator=Validator(), cleanup_executor=_Cleanup(),
        plan_store=plan_store, checkpoint_store=checkpoint_store,
        fence_invalidate=lambda p: fence_calls.append("invalidate"),
        fence_terminate=lambda p: fence_calls.append("terminate"),
        fence_verify=lambda p: fence_calls.append("verify") or True,
        snapshot_provider=lambda jid, phase: {
            "progress": 0.5, "cpu_utilization_delta": 0.5,
            "memory_utilization_delta": 0.5, "progress_delta": 0.5},
    )
    out = coord.execute_plan(plan)
    assert out.current_state == MigrationState.SUCCESS
    assert fence_calls == ["invalidate", "terminate", "verify"]

    # -- post-conditions: one authoritative execution, books balanced --
    final = registry.get("job-1")
    assert final["state"] == "RUNNING"
    assert final["execution_epoch"] == 1
    assert final["active_migration_id"] is None
    ledger.mark_completed(plan.migration_id, "coordinator")
    assert ledger.has_processed(plan.migration_id, "coordinator") is True
    history.enrich(plan.migration_id, {"actual_cost": 0.4, "outcome": "SUCCESS"})
    assert history.get(plan.migration_id)["actual_cost"] == 0.4

    recon = ReconciliationManager(registry, None, _Cleanup(), plan_store=plan_store)
    leftovers = [f for f in recon.reconcile(ReconciliationTrigger.PERIODIC_SWEEP)
                 if f.job_id == "job-1" and f.migration_id == plan.migration_id]
    assert leftovers == []
