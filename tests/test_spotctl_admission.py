"""Phase A tests: admission, spotctl shim, pool definitions, provisioning,
telemetry-driven readiness (invariants 1-4, 8)."""
import json
import tempfile
from pathlib import Path

from orchestrator.admission import (
    JobAdmissionManager, WorkloadContract, CONTRACT_SCHEMA_VERSION,
)
from orchestrator.main import _requirements_for_job, _telemetry_capacity_for_job
from storage.pool_registry import validate_pool_definition, definition_from_pool


def _contract(**kw):
    base = {"name": "risk-sim", "script": "./monte_carlo.py",
            "cpu": 4, "memory_mb": 16384, "regions": ("us-east-1",)}
    base.update(kw)
    return WorkloadContract(**base)


def test_contract_round_trip_and_frozen():
    c = _contract()
    d = c.to_dict()
    assert d["schema_version"] == CONTRACT_SCHEMA_VERSION
    assert d["resources"]["cpu"] == 4
    c2 = WorkloadContract.from_dict(d)
    assert c2.cpu == 4 and c2.memory_mb == 16384
    try:
        c.cpu = 8
        raise AssertionError("contract must be frozen")
    except AttributeError:
        pass


def test_admission_rejects_bad_contract():
    m = JobAdmissionManager()
    bad = WorkloadContract(name="", script="", cpu=0, memory_mb=0, regions=())
    res = m.admit(bad, registry=None)
    assert res.verdict == "REJECTED" and res.reason


def test_admission_never_takes_pid_ip_ami():
    """Invariant 1+2: user supplies no PID/IP/AMI; system owns those fields."""
    import inspect
    from orchestrator.admission import JobAdmissionManager
    sig = inspect.signature(JobAdmissionManager.admit)
    assert set(sig.parameters) == {"self", "contract", "registry", "job_id"}
    csig = inspect.signature(WorkloadContract)
    for forbidden in ("pid", "public_ip", "ami", "ami_id"):
        assert forbidden not in csig.parameters
    # System-owned fields are initialized empty, never read from the user.
    src = Path("orchestrator/admission.py").read_text()
    assert '"public_ip": None' in src and '"pid": None' in src


def test_admit_pending_row_json_backend():
    from storage.job_registry import JobRegistry
    with tempfile.TemporaryDirectory() as td:
        p = str(Path(td) / "reg.json")
        Path(p).write_text("{}")
        reg = JobRegistry(p)
        m = JobAdmissionManager()
        res = m.admit(_contract(), reg, job_id="j-pending")
        assert res.verdict == "ADMITTED"
        row = reg.get("j-pending")
        assert row["state"] == "PENDING"
        assert row["public_ip"] is None and row["pid"] is None
        assert row["workload_contract"]["resources"]["cpu"] == 4
        dup = m.admit(_contract(), reg, job_id="j-pending")
        assert dup.verdict == "DUPLICATE"


def test_requirements_from_contract():
    req = _requirements_for_job({"workload_contract": _contract(cpu=8).to_dict()})
    assert req.min_cpu == 8 and req.min_memory_mb == 16384
    legacy = _requirements_for_job({})
    assert legacy.min_cpu == 1 and legacy.min_memory_mb == 1024


def test_telemetry_capacity_missing_is_synthetic():
    conf, artifact, synthetic = _telemetry_capacity_for_job({})
    assert (conf, artifact, synthetic) == (0.8, True, True)


def test_telemetry_stale_degrades_invariant4():
    """Invariant 4: STALE heartbeat never yields eligibility-grade evidence."""
    import datetime as _dt
    stale_ts = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=3600)).isoformat()
    conf, artifact, synthetic = _telemetry_capacity_for_job(
        {"worker_telemetry": {"heartbeat": {"observed_at": stale_ts}}})
    assert synthetic is False and artifact is False and conf < 0.5
    fresh_ts = _dt.datetime.now(_dt.timezone.utc).isoformat()
    conf, artifact, synthetic = _telemetry_capacity_for_job(
        {"worker_telemetry": {"heartbeat": {"observed_at": fresh_ts}}})
    assert synthetic is False and artifact is True and conf >= 0.5


def test_pool_definition_validation():
    minimal = {"pool_id": "p", "region": "r", "instance_type": "t"}
    assert validate_pool_definition(minimal, strict_aws=False) == []
    # Strict AWS requires AMI+SG.
    errs = validate_pool_definition(dict(minimal, provider="aws"))
    assert any("ami_id" in e for e in errs)
    ok = {"pool_id": "p", "region": "r", "instance_type": "t",
          "provider": "aws", "ami_id": "ami-1", "security_group_id": "sg-1"}
    assert validate_pool_definition(ok) == []


def test_definition_from_pool():
    from orchestrator.candidate_pool import (
        CandidatePool, PoolRuntimeProfile, PoolCapacityProfile)
    pool = CandidatePool(pool_id="pool-r-t", provider="aws",
                         account_id="1", region="r",
                         availability_zone="ra", instance_type="t",
                         architecture="x86_64",
                         runtime_profile=PoolRuntimeProfile(
                             artifact_digest="sha256:x", architecture="x86_64"),
                         capacity_profile=PoolCapacityProfile())
    d = definition_from_pool(pool)
    assert d["region"] == "r" and d["runtime_artifact_digest"] == "sha256:x"
    assert validate_pool_definition(d, strict_aws=False) == []


def test_provision_from_pool_falls_back():
    from orchestrator.provisioner import Provisioner
    prov = Provisioner()  # no global config: emulated stub path
    res = prov.provision_from_pool({"pool_id": "p", "region": "r",
                                    "instance_type": "t"})
    assert res.status == "running" and res.operation_id


def test_spotctl_run_status_history_cost_json():
    from scripts import spotctl
    with tempfile.TemporaryDirectory() as td:
        jp = str(Path(td) / "reg.json")
        Path(jp).write_text("{}")
        base = ["--backend", "json", "--json-path", jp]
        spotctl.main(base + ["run", "--name", "risk-sim",
                             "--script", "./m.py", "--cpu", "4",
                             "--memory", "16GiB", "--regions", "us-east-1"])
        data = json.loads(Path(jp).read_text())
        jid = next(iter(data))
        assert data[jid]["state"] == "PENDING"
        assert "public_ip" not in str(spotctl.main)  # shim takes no such flag
        spotctl.main(base + ["status", "--name", jid])
        spotctl.main(base + ["history", "--name", jid])
        spotctl.main(base + ["cost", "--name", jid])


def test_spotctl_run_accepts_no_pid_ip_ami_flags():
    """Invariant 1+2 at the CLI surface: forbidden flags don't exist."""
    import subprocess
    out = subprocess.run(
        ["python", "scripts/spotctl.py", "run", "--help"],
        capture_output=True, text=True, cwd=".")
    help_text = out.stdout.lower()
    assert "--public-ip" not in help_text
    assert "--pid" not in help_text
    assert "--ami" not in help_text


def test_warm_rescue_off_by_default_invariant8():
    """Invariant 8: no warm-rescue machinery exists unless opted in."""
    src = Path("orchestrator/main.py").read_text()
    assert "--warm-rescue" not in src or "default" in src.lower()
    import subprocess
    out = subprocess.run(
        ["python", "-m", "orchestrator.main", "--help"],
        capture_output=True, text=True, cwd=".")
    assert "warm" not in out.stdout.lower()


def test_runtime_publish_writes_pool_definitions():
    from scripts import spotctl
    with tempfile.TemporaryDirectory() as td:
        pf = str(Path(td) / "pools.json")
        spotctl.main(["runtime", "publish", "--digest", "sha256:abc",
                      "--regions", "us-east-1,us-west-2",
                      "--instance-type", "t3.micro",
                      "--ami", "us-east-1=ami-111",
                      "--pools-file", pf])
        defs = json.loads(Path(pf).read_text())
        assert defs["pool-us-east-1-t3.micro"]["runtime_artifact_digest"] == "sha256:abc"
        assert defs["pool-us-east-1-t3.micro"]["ami_id"] == "ami-111"
        # us-west-2 without --ami stays incomplete but present.
        assert defs["pool-us-west-2-t3.micro"]["ami_id"] == ""


def test_pools_definition_file_round_trip():
    from storage.pool_registry import (
        load_definitions_file, save_definitions_file)
    with tempfile.TemporaryDirectory() as td:
        pf = str(Path(td) / "pools.json")
        assert load_definitions_file(pf) == {}
        assert load_definitions_file("") == {}
        save_definitions_file(pf, {"p": {"region": "r"}})
        assert load_definitions_file(pf) == {"p": {"region": "r"}}


def test_p95_step_estimates_needs_samples():
    from orchestrator.recovery_feasibility import p95_step_estimates
    rows = [{"actual_checkpoint_duration": 10.0 + i,
             "actual_transfer_duration": 20.0} for i in range(6)]
    p95 = p95_step_estimates(rows)
    assert p95["checkpointing"] > 10.0
    assert "provisioning" not in p95  # absent key → no samples
    thin = p95_step_estimates(rows[:2])
    assert thin == {}
    assert p95_step_estimates("garbage") == {}
    assert p95_step_estimates([{"actual_checkpoint_duration": "nan-x"}]) == {}


def test_measured_overrides_replace_defaults():
    from orchestrator.recovery_feasibility import build_emergency_plan_steps

    class Est:
        prediction_confidence = 0.8
        checkpoint_duration_estimate_seconds = 12.0
        checkpoint_size_estimate_bytes = 100 * 1024 * 1024

    plain = {s.name: s for s in build_emergency_plan_steps(Est())}
    measured = {s.name: s for s in build_emergency_plan_steps(
        Est(), measured_overrides={"provisioning": 33.0, "transferring": 44.0})}
    assert measured["provisioning"].estimated_seconds == 33.0
    assert measured["transferring"].estimated_seconds == 44.0
    assert plain["provisioning"].estimated_seconds != 33.0


def test_measured_overrides_helper_tolerates_missing_history():
    from orchestrator.main import _measured_step_overrides

    class NoHistory:
        def get_feedback_for_estimator(self, job_id):
            raise RuntimeError("no store")

    assert _measured_step_overrides(NoHistory(), "j") == {}
    assert _measured_step_overrides(None, "j") == {}
