"""P0 wiring regression tests for orchestrator/main.py.

Guards the three 0%-coverage bugs plus follow-ups:
1. First-tick NameError (Pool*/Capacity* imports).
2. Emergency-path TypeError (missing plan_steps) + Option B step modeling.
3. History silently unpersisted (storage never wired).
4. Placement-engine wired (rank orders candidates; plan uses chosen target).
5. Pool definitions persisted to PoolRegistryStore.
6. Invariants 4-8: structured history, durability lookup, per-candidate
   feasibility, detection_lag, emergency target remap.
7. PA-8: coordinator provisions from the pool definition when available.
"""
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from orchestrator.candidate_pool import (
    CandidatePool, PoolRuntimeProfile, PoolCapacityProfile,
)
from orchestrator.candidate_compatibility import (
    CompatibilityAssessment, CompatibilityStatus, CompatibilityDimensionResult,
    CompatibilityEngine,
)
from orchestrator.candidate_readiness import (
    ReadinessEngine, ReadinessAssessment, ReadinessStatus, ReadinessCheck,
    CapacityEvidence, CapacityStatus, EvidenceSource,
    IAMReadiness, NetworkReadiness, StorageReadiness,
)
from orchestrator.candidate_placement import (
    PlacementEngine, PlacementPolicy, PlacementInput,
)
from orchestrator.workload_requirements import WorkloadRequirements, GPURequirement
from orchestrator.recovery_feasibility import (
    RecoveryFeasibilityEngine, FeasibilityStatus, build_emergency_plan_steps,
)
from orchestrator.workload_estimator import WorkloadEstimate
from orchestrator.migration_history import MigrationHistory
from storage.history_store import HistoryStore
from storage.pool_registry import PoolRegistryStore


def _est():
    return WorkloadEstimate(
        job_id="j1", execution_epoch=0, progress=0.5,
        remaining_runtime_seconds=3600,
        expected_completion_at=None, prediction_confidence=0.8,
        checkpoint_size_estimate_bytes=10 * 1024 ** 2,
        checkpoint_duration_estimate_seconds=10.0,
        estimated_at=datetime.now(timezone.utc), estimator_version="v",
        model_version="v", observation_snapshot_version="1",
    )


def _compat():
    return CompatibilityAssessment(
        assessment_id="c1", pool_id="pool-a",
        workload_requirements_version="1",
        status=CompatibilityStatus.COMPATIBLE,
        dimensions=[CompatibilityDimensionResult("cpu", CompatibilityStatus.COMPATIBLE)],
        assessed_at=datetime.now(timezone.utc),
    )


def _ready():
    return ReadinessAssessment(
        assessment_id="r1", pool_id="pool-a", status=ReadinessStatus.READY,
        checks=[ReadinessCheck("iam", ReadinessStatus.READY)],
        capacity_evidence=CapacityEvidence(
            status=CapacityStatus.AVAILABLE,
            source=EvidenceSource.HISTORICAL_PROVISIONING,
            observed_at=datetime.now(timezone.utc), confidence=0.9,
            provisioning_success_rate=0.9, sample_count=10),
        iam_readiness=IAMReadiness(True, True, True, True),
        network_readiness=NetworkReadiness(True, True, True, True),
        storage_readiness=StorageReadiness(True, True, True),
        artifact_available=True, runtime_ready=True,
    )


def test_first_tick_builds_pools():
    """Reproduces main.py per-job loop pool construction (Bug #1)."""
    # main.py must expose these names (import guard against NameError).
    import orchestrator.main as main_mod
    for name in ("PoolRuntimeProfile", "PoolCapacityProfile",
                 "CapacityEvidence", "CapacityStatus", "EvidenceSource"):
        assert hasattr(main_mod, name), f"main.py missing import: {name}"

    # Exact construction block from the loop — must not raise.
    pool = CandidatePool(
        pool_id="pool-us-east-1-c6i.large",
        provider="aws", account_id="123456789", region="us-east-1",
        availability_zone="us-east-1a", instance_type="c6i.large",
        architecture="x86_64",
        runtime_profile=PoolRuntimeProfile(
            artifact_digest="sha256:abc123", architecture="x86_64"),
        capacity_profile=PoolCapacityProfile(max_instances=10),
    )
    assert pool.region == "us-east-1"

    evidence = CapacityEvidence(
        status=CapacityStatus.AVAILABLE,
        source=EvidenceSource.HISTORICAL_PROVISIONING,
        observed_at=datetime.now(timezone.utc), confidence=0.8,
        provisioning_success_rate=0.9, sample_count=10,
    )
    compat = CompatibilityEngine().assess(
        WorkloadRequirements(
            cpu_architecture="x86_64", min_cpu=1, min_memory_mb=1024,
            gpu=GPURequirement(required=False), reconnectable=True,
        ),
        pool,
    )
    assert compat.status.value in ("COMPATIBLE", "INCOMPATIBLE")
    ready = ReadinessEngine().assess(
        WorkloadRequirements(
            cpu_architecture="x86_64", min_cpu=1, min_memory_mb=1024,
            gpu=GPURequirement(required=False), reconnectable=True,
        ), pool,
        iam_ready=IAMReadiness(True, True, True, True),
        network_ready=NetworkReadiness(True, True, True, True),
        storage_ready=StorageReadiness(True, True, True),
        capacity_evidence=evidence, artifact_available=True,
    )
    assert ready.status.value in ("READY", "PREPARABLE", "NOT_READY", "UNKNOWN")


def test_emergency_feasibility_no_typeerror():
    """Emergency call site passes real plan_steps (Bug #2, Option B)."""
    engine = RecoveryFeasibilityEngine()
    # Legacy 4-arg call still must not raise TypeError (Option A compat).
    legacy = engine.evaluate(_est(), _compat(), _ready(), 120.0)
    assert legacy.status in (FeasibilityStatus.FEASIBLE,
                             FeasibilityStatus.INFEASIBLE,
                             FeasibilityStatus.INSUFFICIENT_EVIDENCE)
    assert legacy.critical_path_seconds == 0.0

    # Option B: real step estimates carry a nonzero critical path.
    steps = build_emergency_plan_steps(_est())
    assert len(steps) == 10
    assert all(s.estimated_seconds is not None for s in steps)
    result = engine.evaluate(_est(), _compat(), _ready(), 1200.0, steps)
    assert result.status == FeasibilityStatus.FEASIBLE
    assert result.critical_path_seconds > 0


def test_emergency_steps_unknown_checkpoint_yields_insufficient_evidence():
    """Unknown checkpoint duration must not produce a false FEASIBLE."""
    est = _est()
    est.checkpoint_duration_estimate_seconds = None
    steps = build_emergency_plan_steps(est)
    ckpt = next(s for s in steps if s.name == "checkpointing")
    assert ckpt.estimated_seconds is None
    result = RecoveryFeasibilityEngine().evaluate(
        est, _compat(), _ready(), 30.0, steps)
    # 30s deadline can't fit an unknown critical path (inf) -> not FEASIBLE.
    assert result.status in (FeasibilityStatus.INFEASIBLE,
                             FeasibilityStatus.INSUFFICIENT_EVIDENCE)


def _fake_plan(mid="mig-1"):
    return SimpleNamespace(
        migration_id=mid, job_id="j1", execution_epoch=0,
        regime=SimpleNamespace(value="EMERGENCY"),
        source_candidate_id="pool-a", target_candidate_id="pool-b",
        steps=[],
    )


def _fake_decision():
    return SimpleNamespace(
        decision=SimpleNamespace(value="RECOVER"),
        regime=SimpleNamespace(value="EMERGENCY"),
        reason="test", confidence=0.9,
    )


def test_history_wired_to_durable_store():
    """MigrationHistory must persist via HistoryStore.save (Bug #3)."""
    store = HistoryStore()
    history = MigrationHistory(storage=store)
    assert history.storage is store

    mid = history.record_start(_fake_plan(), _fake_decision(), None, None)
    assert store.get(mid)["migration_id"] == mid

    history.record_completion(mid, "SUCCESS")
    doc = store.get(mid)
    assert doc["migration_id"] == mid

    # main.py source must late-bind the durable store (regression pin).
    src = Path("orchestrator/main.py").read_text()
    assert "history.storage = history_store" in src
    assert "MigrationHistory(storage=history_store)" in src or \
        "history.storage = history_store" in src


def test_placement_wired_and_orders_candidates():
    """PlacementEngine.rank is wired: cheaper eligible candidate wins.

    Mirrors the main-loop input construction (compat/ready/cost/risk).
    """
    src = Path("orchestrator/main.py").read_text()
    assert "placement_engine = PlacementEngine(" in src
    assert "placement_engine.rank(" in src
    # Plan must use the analysis matching the chosen target, not [0].
    assert "_chosen_analysis" in src
    assert "candidate_analysis=candidate_analyses[0]" not in src

    eng = PlacementEngine(PlacementPolicy(version="v3"))

    def _inp(pool, total_cost):
        return PlacementInput(
            pool_id=pool,
            compatibility_status=CompatibilityStatus.COMPATIBLE,
            readiness_status=ReadinessStatus.READY,
            compatibility_dimensions={"cpu": True, "mem": True},
            readiness_checks=[{"status": "READY"}],
            cost_analysis={"expected_total_cost": total_cost},
            risk_analysis={"interruption_probability": 0.2},
            topology={}, capacity_confidence=0.9,
            pool_lifecycle="ACTIVE",
        )

    rec = eng.rank([_inp("r-bad", 9.0), _inp("r-good", 3.0)], regime="ARBITRAGE")
    assert rec.selected_candidate_id == "r-good"
    # Emergency excludes DEGRADED pools.
    rec_em = eng.rank([
        _inp("r-degraded", 1.0), _inp("r-active", 5.0),
    ], regime="EMERGENCY")
    rec_em.ranked_candidates[0].pool_id  # sorted deterministically
    deg = PlacementInput(
        pool_id="r-degraded",
        compatibility_status=CompatibilityStatus.COMPATIBLE,
        readiness_status=ReadinessStatus.READY,
        compatibility_dimensions={"cpu": True},
        readiness_checks=[{"status": "READY"}],
        cost_analysis={"expected_total_cost": 1.0},
        risk_analysis={"interruption_probability": 0.1},
        topology={}, capacity_confidence=0.9,
        pool_lifecycle="DEGRADED",
    )
    rec2 = eng.rank([deg, _inp("r-active", 5.0)], regime="EMERGENCY")
    assert rec2.selected_candidate_id == "r-active"


def test_pool_definitions_persisted():
    """PoolRegistryStore round-trips definitions; main.py persists per pool."""
    store = PoolRegistryStore()
    doc = store.put_pool("pool-r-a", {"region": "r", "lifecycle": "ACTIVE"}, version=1)
    assert doc["pool_id"] == "pool-r-a"
    assert store.get_pool("pool-r-a")["definition"]["region"] == "r"
    try:
        store.put_pool("pool-r-a", {"region": "r"}, version=1)
        raise AssertionError("expected version conflict")
    except RuntimeError:
        pass

    src = Path("orchestrator/main.py").read_text()
    assert "pool_store.put_pool(" in src
    assert "_persisted_pools" in src


def test_history_structured_results_and_legacy_tolerance():
    """Invariant 7 (part): structured results preserved, legacy tolerated."""
    from orchestrator.migration_history import (
        MigrationHistory, _normalize_step_result, _step_result_dict,
    )
    stored, schema, legacy = _normalize_step_result({"size_bytes": 123})
    assert stored == {"size_bytes": 123} and not legacy
    assert _step_result_dict({"result": stored}) == {"size_bytes": 123}

    # Legacy string row stays readable.
    assert _step_result_dict({"result": "CheckpointResult(x)"}) == {}
    revived, _, was_legacy = _normalize_step_result('{"size_bytes": 5}')
    assert revived == {"size_bytes": 5} and was_legacy

    h = MigrationHistory()
    mid = h.record_start(_fake_plan(), _fake_decision(), None, None)
    h.record_step(mid, "checkpointing", "SUCCEEDED",
                  {"size_bytes": 10 * 1024 ** 2}, duration=31.4)
    from orchestrator.migration_history import MigrationOutcome
    h.record_completion(mid, MigrationOutcome.SUCCESS)
    fb = h.get_feedback_for_estimator("j1")[0]
    assert fb["actual_checkpoint_size"] == 10 * 1024 ** 2
    assert fb["actual_checkpoint_duration"] == 31.4
    assert fb["legacy_result"] is False


def test_checkpoint_durability_lookup_never_infers():
    """Invariant 5: UNKNOWN durability never becomes TRUE."""
    from orchestrator.main import query_checkpoint_durable
    from storage.checkpoint_store import CheckpointStore
    store = CheckpointStore()
    assert query_checkpoint_durable(store, "job-x") is False
    assert query_checkpoint_durable(None, "job-x") is False
    assert "checkpoint_durable=True" not in Path("orchestrator/main.py").read_text()
    assert "query_checkpoint_durable(" in Path("orchestrator/main.py").read_text()


def test_checkpoint_store_latest_durable():
    """Only DURABLE/VALIDATED checkpoints satisfy the lookup."""
    from storage.checkpoint_store import CheckpointStore
    from orchestrator.models_v2 import CheckpointRef
    store = CheckpointStore()
    store.put(CheckpointRef(checkpoint_id="c-local", lineage_id="j1",
                            sequence=1, execution_epoch=0, durability="LOCAL"))
    assert store.latest_durable("j1") is None
    store.put(CheckpointRef(checkpoint_id="c-dur", lineage_id="j1",
                            sequence=2, execution_epoch=0, durability="DURABLE"))
    assert store.latest_durable("j1")["checkpoint_id"] == "c-dur"
    assert store.latest_durable("nope") is None


def test_per_candidate_feasibility_wired():
    """Invariant 6: emergency evaluates every candidate; none → ABANDON."""
    src = Path("orchestrator/main.py").read_text()
    assert "_feas_by_pool" in src
    assert "_survivors" in src
    assert "no feasible emergency candidate" in src
    # Feasibility of the single [0] candidate must be gone.
    assert "compat_assessments[emergency_pools[0]],\n                    ready_assessments[emergency_pools[0]],\n                    _deadline,\n                    build_emergency_plan_steps" not in src


def test_detection_lag_recorded():
    """Invariant P0-4: ingestion stamps noticed_at + detection_lag."""
    from datetime import timedelta
    from orchestrator.interruption_ingestion import ingest_interruption
    past = (datetime.now(timezone.utc) - timedelta(seconds=42)).isoformat()
    evt = ingest_interruption(
        {"detected_at": past, "source": "imds"},
        job_id="j1", execution_epoch=0, instance_id="i-1",
        source_region="us-east-1")
    assert evt is not None
    lag = evt.payload["detection_lag_seconds"]
    assert 30.0 < lag < 300.0
    assert evt.payload["noticed_at"]
    assert "detection_lag" in Path("orchestrator/main.py").read_text()


def test_emergency_target_remapped_to_region_key():
    """Assessment pool_ids must not KeyError the region-keyed lookups."""
    src = Path("orchestrator/main.py").read_text()
    assert "decision.target_candidate_id not in compat_assessments" in src


class _DualPathProvisioner:
    """Fake exposing both legacy and pool-definition provision paths."""

    def __init__(self):
        self.via_pool = []
        self.via_legacy = []

    def provision_with_operation(self, candidate_id, operation_id=None, tags=None):
        from orchestrator.provisioner import ProvisionResult
        self.via_legacy.append((candidate_id, operation_id))
        return ProvisionResult(instance_id="i-legacy", public_ip="1.1.1.1",
                               public_dns="h", status="running",
                               operation_id=operation_id or "")

    def provision_from_pool(self, definition, operation_id=None, tags=None):
        from orchestrator.provisioner import ProvisionResult
        self.via_pool.append((dict(definition), operation_id))
        return ProvisionResult(instance_id="i-pool", public_ip="2.2.2.2",
                               public_dns="h", status="running",
                               operation_id=operation_id or "")


def _provisioning_coordinator(provisioner, provider):
    from orchestrator.migration_coordinator import MigrationCoordinator
    return MigrationCoordinator(
        registry=SimpleNamespace(get=lambda jid: {}, transition=lambda *a, **k: {}),
        provisioner=provisioner,
        checkpoint_manager=SimpleNamespace(),
        transfer_manager=SimpleNamespace(),
        validator=SimpleNamespace(),
        cleanup_executor=SimpleNamespace(),
        step_timeout_seconds=30.0,
        pool_definition_provider=provider,
    )


def _provisioning_plan(target="us-west-2"):
    from orchestrator.migration_planner import MigrationPlanner
    from orchestrator.policy_engine import (
        PolicyDecision, Decision, MigrationRegime)
    plan = MigrationPlanner().create_plan(
        job_id="j1", execution_epoch=0, regime=MigrationRegime.ARBITRAGE,
        policy_decision=PolicyDecision(
            decision=Decision.MIGRATE, regime=MigrationRegime.ARBITRAGE,
            reason="t", target_candidate_id=target, confidence=0.9,
            metadata={}),
        source_pool_id="pool-s", target_pool_id=target,
        workload_estimate=_est())
    return plan


def test_coordinator_prefers_pool_definition():
    """PA-8/invariant 2: execution uses the pool definition when resolvable."""
    from orchestrator.migration_coordinator import MigrationExecutionState
    prov = _DualPathProvisioner()
    definition = {"pool_id": "pool-us-west-2-t3.micro", "region": "us-west-2",
                  "ami_id": "ami-xyz", "security_group_id": "sg-1"}
    coord = _provisioning_coordinator(
        prov, provider=lambda target: definition if target == "us-west-2" else None)
    coord._execution_state = MigrationExecutionState(
        plan=_provisioning_plan("us-west-2"))
    coord._execute_provisioning()
    assert len(prov.via_pool) == 1
    assert prov.via_pool[0][0]["ami_id"] == "ami-xyz"
    assert prov.via_legacy == []
    assert coord._execution_state.target_instance_id == "i-pool"


def test_coordinator_falls_back_to_legacy_path():
    """PA-8: no definition (or no provider) keeps the legacy path working."""
    from orchestrator.migration_coordinator import MigrationExecutionState
    # No definition resolvable -> legacy.
    prov = _DualPathProvisioner()
    coord = _provisioning_coordinator(prov, provider=lambda target: None)
    coord._execution_state = MigrationExecutionState(
        plan=_provisioning_plan("us-west-2"))
    coord._execute_provisioning()
    assert prov.via_pool == []
    assert len(prov.via_legacy) == 1
    # No provider at all (all existing callers) -> legacy.
    prov2 = _DualPathProvisioner()
    coord2 = _provisioning_coordinator(prov2, provider=None)
    coord2._execution_state = MigrationExecutionState(
        plan=_provisioning_plan("us-west-2"))
    coord2._execute_provisioning()
    assert prov2.via_pool == []
    assert len(prov2.via_legacy) == 1


def test_wired_coordinator_passes_pool_provider():
    """main.py must hand the pool store to the coordinator (PA-8 wiring)."""
    src = Path("orchestrator/main.py").read_text()
    assert "pool_definition_provider=_pool_definition_for" in src
    assert "def _pool_definition_for(target_pool_id" in src


# -- Later pass: PENDING admission lifecycle (invariants 1-3) --

import json as _json
import time as _time


def _pending_job(job_id="adm-1", **over):
    job = {
        "job_id": job_id, "state": "PENDING", "region": "us-east-1",
        "public_ip": None, "pid": None, "version": 0,
        "execution_epoch": 0, "workload_type": "batch",
        "workload_contract": {
            "name": job_id, "script": "job.py",
            "resources": {"cpu": 1, "memory_mb": 512,
                          "gpu": False, "storage_gb": 10},
            "placement": {"regions": ["us-east-1", "us-west-2"]},
        },
    }
    job.update(over)
    return job


class _PendingRegistry:
    """Dict registry enforcing the real v2 lifecycle on transitions."""

    def __init__(self, job):
        self.rows = {job["job_id"]: dict(job)}
        self.calls = []

    def get(self, job_id):
        return dict(self.rows[job_id])

    def transition(self, job_id, to_state, **attrs):
        from storage.v2_transitions import is_v2_transition_allowed
        cur = self.rows[job_id].get("state")
        ok = is_v2_transition_allowed("job", cur, to_state)
        if to_state == cur == "PENDING":
            ok = True  # mirrors the admission staging carve-out
        if not ok:
            raise RuntimeError(f"Invalid state transition: {cur} -> {to_state}")
        self.rows[job_id].update(attrs)
        self.rows[job_id]["state"] = to_state
        self.rows[job_id]["version"] = self.rows[job_id].get("version", 0) + 1
        self.calls.append((to_state, dict(attrs)))
        return dict(self.rows[job_id])


class _PendingProvisioner:
    def __init__(self, ip="10.9.9.9", iid="i-adm-1"):
        self.definitions = []
        self.legacy = []
        self.ip, self.iid = ip, iid

    def provision_from_pool(self, definition, operation_id=None, tags=None):
        from orchestrator.provisioner import ProvisionResult
        self.definitions.append(dict(definition))
        return ProvisionResult(instance_id=self.iid, public_ip=self.ip,
                               public_dns="h", status="running",
                               operation_id=operation_id or "")

    def provision_with_operation(self, candidate_id, operation_id=None, tags=None):
        from orchestrator.provisioner import ProvisionResult
        self.legacy.append(candidate_id)
        return ProvisionResult(instance_id=self.iid, public_ip=self.ip,
                               public_dns="h", status="running",
                               operation_id=operation_id or "")


class _PendingLimiter:
    def __init__(self, ok=True):
        self.ok, self.releases = ok, 0

    def acquire(self, timeout=0):
        return self.ok

    def release(self):
        self.releases += 1


def _pending_deps(job, **over):
    from orchestrator.main import _handle_pending_job
    from orchestrator.candidate_compatibility import CompatibilityEngine
    from orchestrator.candidate_placement import PlacementEngine, PlacementPolicy
    from storage.pool_registry import PoolRegistryStore
    reg = _PendingRegistry(job)
    prov = _PendingProvisioner()
    store = PoolRegistryStore()
    for r in ("us-east-1", "us-west-2"):
        store.put_pool(f"pool-{r}-t3.micro", {
            "pool_id": f"pool-{r}-t3.micro", "provider": "aws",
            "region": r, "ami_id": f"ami-{r}", "lifecycle": "ACTIVE"}, version=1)
    kw = dict(
        prices={"us-east-1": {"price": 0.01}, "us-west-2": {"price": 0.02}},
        instance_type="t3.micro", pool_store=store,
        compatibility_engine=CompatibilityEngine(),
        placement_engine=PlacementEngine(PlacementPolicy(version="v3")),
        provisioner=prov, rate_limiter=_PendingLimiter(),
        registry=reg, transport_for=lambda host: None,
        pending_attempt={}, tele_attempt={}, now=_time.time(), log=None,
    )
    kw.update(over)
    return _handle_pending_job, reg, prov, kw


def test_pending_provisions_without_user_facts():
    """Invariants 1-3: admitted job gains IP/instance from provisioning."""
    handler, reg, prov, kw = _pending_deps(_pending_job())
    job = reg.get("adm-1")
    handler(job, "adm-1", "us-east-1", **kw)
    row = reg.get("adm-1")
    assert row["state"] == "PENDING"  # not RUNNING yet: no heartbeat
    assert row["public_ip"] == "10.9.9.9"
    assert row["provisioned_instance_id"] == "i-adm-1"
    assert row["pid"] is None  # never invented
    assert len(prov.definitions) == 1  # pool definition, not global config
    assert prov.legacy == []


def _heartbeat_transport(pid=4242, age_s=5.0):
    import json as _j

    class _T:
        def run_command(self, command, timeout=60.0):
            at = (datetime.now(timezone.utc).isoformat())
            if age_s is not None:
                from datetime import timedelta as _td
                at = (datetime.now(timezone.utc) - _td(seconds=age_s)).isoformat()
            return {"returncode": 0, "stdout": _j.dumps({
                "schema": "heartbeat/v1", "job_id": "adm-1",
                "instance_id": "i-adm-1", "execution_epoch": 0,
                "pid": pid, "observed_at": at})}
    return _T()


def test_pending_promotes_on_fresh_heartbeat():
    """Telemetry-discovered PID/host promote PENDING -> RUNNING."""
    job = _pending_job(public_ip="10.9.9.9", instance_id="i-adm-1",
                       provisioned_instance_id="i-adm-1")
    handler, reg, prov, kw = _pending_deps(
        job, transport_for=lambda host: _heartbeat_transport())
    handler(reg.get("adm-1"), "adm-1", "us-east-1", **kw)
    row = reg.get("adm-1")
    assert row["state"] == "RUNNING"
    assert row["pid"] == 4242
    assert row["instance_id"] == "i-adm-1"
    assert prov.definitions == [] and prov.legacy == []  # no re-provision


def test_pending_stale_never_promotes():
    """Invariant 4: STALE heartbeat keeps the job PENDING."""
    job = _pending_job(public_ip="10.9.9.9", instance_id="i-adm-1",
                       provisioned_instance_id="i-adm-1")
    handler, reg, prov, kw = _pending_deps(
        job, transport_for=lambda host: _heartbeat_transport(age_s=3600.0))
    handler(reg.get("adm-1"), "adm-1", "us-east-1", **kw)
    assert reg.get("adm-1")["state"] == "PENDING"
    assert reg.get("adm-1")["pid"] is None


def test_pending_handler_never_raises():
    """Dead transports, failed provisions and failed Telemetries skip."""
    job = _pending_job()

    class _Boom:
        def run_command(self, *a, **k):
            raise RuntimeError("no network")

    class _BoomProv(_PendingProvisioner):
        def provision_from_pool(self, *a, **k):
            raise RuntimeError("quota")

    handler, reg, prov, kw = _pending_deps(job)
    kw["transport_for"] = lambda host: _Boom()
    handler(reg.get("adm-1"), "adm-1", "us-east-1", **kw)  # provision ok here
    # Now break provisioning too: fresh job, boom provisioner.
    handler2, reg2, _, kw2 = _pending_deps(_pending_job("adm-2"))
    kw2["provisioner"] = _BoomProv()
    handler2(reg2.get("adm-2"), "adm-2", "us-east-1", **kw2)
    row = reg2.get("adm-2")
    assert row["state"] == "PENDING"
    assert row["admission_status"] == "provision-failed"


def test_pending_lifecycle_in_real_registry(tmp_path):
    """PENDING rows move in the real JSON backend (no stranded states)."""
    from storage.job_registry import JobRegistry
    from orchestrator.admission import JobAdmissionManager, WorkloadContract
    p = tmp_path / "registry.json"
    p.write_text(_json.dumps({}))
    reg = JobRegistry(str(p))
    res = JobAdmissionManager(default_regions=["us-east-1"]).admit(
        WorkloadContract(name="e2e-adm", script="job.py",
                         regions=("us-east-1",)), reg, job_id="e2e-adm")
    assert res.verdict == "ADMITTED"
    assert reg.get("e2e-adm")["state"] == "PENDING"
    reg.transition("e2e-adm", "PENDING", public_ip="10.1.1.1",
                   provisioned_instance_id="i-1")
    assert reg.get("e2e-adm")["public_ip"] == "10.1.1.1"
    reg.transition("e2e-adm", "RUNNING", pid=4242)
    assert reg.get("e2e-adm")["state"] == "RUNNING"
    assert reg.get("e2e-adm")["pid"] == 4242


def test_multijob_states_include_pending():
    """Admitted jobs must be visible to the loop without CLI overrides."""
    src = Path("orchestrator/main.py").read_text()
    assert '"PENDING", "RUNNING"' in src
    assert 'default="PENDING,RUNNING"' in src


def _wiring_plan(job_id="j1", regime="ARBITRAGE", target="us-west-2"):
    from orchestrator.migration_planner import MigrationPlanner
    from orchestrator.policy_engine import (
        PolicyDecision, Decision, MigrationRegime)
    return MigrationPlanner().create_plan(
        job_id=job_id, execution_epoch=0,
        regime=getattr(MigrationRegime, regime),
        policy_decision=PolicyDecision(
            decision=Decision.MIGRATE, regime=getattr(MigrationRegime, regime),
            reason="t", target_candidate_id=target, confidence=0.9,
            metadata={}),
        source_pool_id="pool-s", target_pool_id=target,
        workload_estimate=_est())


def test_plan_admission_and_outcome_hygiene():
    """S16 seam: admitted plans are visible; marked plans leave the active set."""
    from orchestrator.main import _admit_plan_to_store, _mark_plan_outcome
    from storage.plan_store import PlanStore, PlanExistsError
    import pytest
    store = PlanStore()
    plan = _wiring_plan()
    _admit_plan_to_store(store, plan)
    assert len(store.list_active()) == 1
    # Duplicate admission (retried tick) is tolerated, never duplicated.
    _admit_plan_to_store(store, plan)
    assert len(store.list_active()) == 1
    # Lookup finds the pre-fence arbitrage plan for pre-emption.
    found = store.find_preemptible_plan("j1", plan.migration_id)
    assert found is not None and found["plan_id"] == plan.plan_id
    # Terminal marking removes it from the active set (write-once).
    _mark_plan_outcome(store, plan, "SUCCEEDED")
    assert store.list_active() == []
    assert store.find_preemptible_plan("j1", plan.migration_id) is None
    with pytest.raises(Exception):
        store.mark_plan_outcome(plan.plan_id, "FAILED")
    # EMERGENCY plans are never pre-emption candidates.
    em = _wiring_plan(regime="EMERGENCY")
    _admit_plan_to_store(store, em)
    assert store.find_preemptible_plan("j1", em.migration_id) is None
    # Unknown plan/outcome surface loudly (fail-closed, not silent).
    with pytest.raises(Exception):
        store.mark_plan_outcome("nope", "SUCCEEDED")
    with pytest.raises(Exception):
        store.mark_plan_outcome(em.plan_id, "BOGUS")
    with pytest.raises(Exception):
        store.put_plan_doc({})


def test_preemptible_lookup_scoped_to_job_attempt():
    """Lookup never crosses job or migration-attempt boundaries."""
    from orchestrator.main import _admit_plan_to_store
    from storage.plan_store import PlanStore
    store = PlanStore()
    plan = _wiring_plan(job_id="j1")
    _admit_plan_to_store(store, plan)
    assert store.find_preemptible_plan("other-job", plan.migration_id) is None
    assert store.find_preemptible_plan("j1", "other-attempt") is None


def test_requirements_carry_runtime_drift_identity():
    """Phase B: contract digest/regions/secrets reach compatibility."""
    from orchestrator.main import _requirements_for_job
    req = _requirements_for_job({
        "job_id": "drift-1",
        "workload_contract": {
            "resources": {"cpu": 1, "memory_mb": 512},
            "runtime": {"artifact_digest": "sha256:real"},
            "placement": {"regions": ["us-west-2"]},
            "required_secret_refs": ["db/password"],
        },
    })
    assert req.runtime_artifact_digest == "sha256:real"
    assert req.placement.allowed_regions == ["us-west-2"]
    assert req.required_secret_refs == ["db/password"]
    # Drift fails deterministically at compatibility time.
    from orchestrator.candidate_compatibility import CompatibilityEngine
    from orchestrator.candidate_pool import (
        CandidatePool, PoolRuntimeProfile, PoolCapacityProfile)
    pool = CandidatePool(
        pool_id="p", provider="aws", account_id="1", region="us-west-2",
        availability_zone="us-west-2a", instance_type="t3.micro",
        architecture="x86_64",
        runtime_profile=PoolRuntimeProfile(
            artifact_digest="sha256:stale", architecture="x86_64"),
        capacity_profile=PoolCapacityProfile(max_instances=10))
    dims = {d.dimension: d.status.value
            for d in CompatibilityEngine().assess(req, pool).dimensions}
    assert dims["runtime"] == "INCOMPATIBLE"
    assert dims["region_az"] == "COMPATIBLE"
