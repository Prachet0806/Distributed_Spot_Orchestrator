"""Phase 4: validator L1-L4 verdicts + reconciliation lifecycle/gate/remediation."""
from datetime import datetime

import pytest

from orchestrator.validator import Validator, ValidationResult, ValidationLevel
from orchestrator.reconciliation_manager import (
    ReconciliationManager, ReconciliationTrigger, MismatchType,
    ReconciliationAction, ReconnectionAction, FindingStatus,
)


def _snaps():
    src = {"progress": 0.5, "cpu_utilization_delta": 0.5,
           "memory_utilization_delta": 0.4, "progress_delta": 0.5}
    tgt = {"progress": 0.51, "cpu_utilization_delta": 0.52,
           "memory_utilization_delta": 0.41, "progress_delta": 0.51}
    return src, tgt


def test_valid_restore_passes_l1_l2_l3_and_v2_verdict():
    src, tgt = _snaps()
    rep = Validator().validate("m1", "j1", 2, src, tgt, expected_epoch=2,
                               checkpoint_lineage={"lineage_valid": True})
    assert rep.overall_result == ValidationResult.PASSED
    assert rep.epoch_match and rep.lineage_valid
    assert rep.to_v2_verdict() == ValidationResult.VALID.value
    v2 = Validator().to_v2_report(rep)
    assert str(v2.result.value) == "VALID"
    assert v2.level_reached.value == "L3"


def test_missing_snapshots_are_inconclusive_not_invalid():
    rep = Validator().validate("m1", "j1", 1)
    assert rep.overall_result == ValidationResult.PARTIAL
    assert rep.to_v2_verdict() == ValidationResult.INCONCLUSIVE.value


def test_tolerance_breach_is_invalid():
    src, tgt = _snaps()
    tgt["cpu_utilization_delta"] = 5.0
    rep = Validator().validate("m1", "j1", 1, src, tgt)
    assert rep.overall_result == ValidationResult.FAILED
    assert rep.to_v2_verdict() == ValidationResult.INVALID.value


def test_epoch_mismatch_fails_despite_healthy_process():
    src, tgt = _snaps()
    rep = Validator().validate("m1", "j1", 3, src, tgt, expected_epoch=2)
    assert rep.epoch_match is False
    assert rep.overall_result == ValidationResult.FAILED
    assert rep.to_v2_verdict() == ValidationResult.INVALID.value


def test_lineage_invalid_fails():
    src, tgt = _snaps()
    rep = Validator().validate("m1", "j1", 1, src, tgt,
                               checkpoint_lineage={"lineage_valid": False})
    assert rep.lineage_valid is False
    assert rep.overall_result == ValidationResult.FAILED


def test_strict_l4_opt_in_only():
    snap = {"a": 1, "b": [1, 2]}
    other = {"a": 1, "b": [1, 3]}
    # Default: L4 not evaluated.
    rep = Validator().validate("m1", "j1", 1, snap, dict(snap))
    assert not [c for c in rep.checks if c.level == ValidationLevel.L4_STRICT]
    # Opt-in equal → pass; unequal → fail.
    assert Validator().validate(
        "m1", "j1", 1, snap, dict(snap),
        strict_determinism=True).overall_result == ValidationResult.PASSED
    assert Validator().validate(
        "m1", "j1", 1, snap, other,
        strict_determinism=True).overall_result == ValidationResult.FAILED


def test_versioned_tolerance_contract_shape():
    src, tgt = _snaps()
    contract = {"version": "tol-7", "minimum_level": "L3",
                "strict_determinism": False,
                "metrics": {"cpu_utilization_delta": {"tolerance_type": "RELATIVE", "value": 0.5},
                            "memory_utilization_delta": {"tolerance_type": "RELATIVE", "value": 0.5},
                            "progress_delta": {"tolerance_type": "RELATIVE", "value": 0.5}}}
    rep = Validator().validate("m1", "j1", 1, src, tgt, tolerance_override=contract)
    assert rep.contract_version == "tol-7"
    assert rep.overall_result == ValidationResult.PASSED


def test_failing_probe_fails_l1():
    v = Validator(infra_probe=lambda **kw: {"passed": False, "target_reachable": False})
    rep = v.validate("m1", "j1", 1, *_snaps())
    assert rep.overall_result == ValidationResult.FAILED

    def _boom(**kw):
        raise RuntimeError("probe exploded")
    rep2 = Validator(app_probe=_boom).validate("m1", "j1", 1, *_snaps())
    assert rep2.overall_result == ValidationResult.FAILED


# -- reconciliation --
class _Reg:
    def __init__(self, jobs):
        self.jobs = {j["job_id"]: dict(j) for j in jobs}
        self.gated = []

    def get(self, job_id):
        return dict(self.jobs[job_id])

    def transition(self, job_id, to_state, **kw):
        self.jobs[job_id]["state"] = to_state
        self.gated.append((job_id, to_state))
        return dict(self.jobs[job_id])

    def list_by_state(self, state):
        return [dict(j) for j in self.jobs.values() if j.get("state") == state]


class _Infra:
    def __init__(self, states):
        self.states = states

    def get_instance_state(self, instance_id):
        return self.states.get(instance_id, "RUNNING")


class _Cleanup:
    def __init__(self):
        self.calls = []

    def cleanup_orphan(self, resource_id, resource_type="instance"):
        self.calls.append((resource_id, resource_type))
        return True


def _job(job_id="j1", state="RUNNING", epoch=0, active=None, inst="i-1"):
    return {"job_id": job_id, "state": state, "execution_epoch": epoch,
            "active_migration_id": active, "instance_id": inst}


def test_taxonomy_and_legacy_alias():
    assert MismatchType.SPLIT_BRAIN.value == "SPLIT_BRAIN"
    assert MismatchType.SOURCE_STILL_ALIVE_AFTER_FENCE.value
    assert MismatchType.DANGLING_MIGRATION_REFERENCE.value
    assert ReconnectionAction is ReconciliationAction  # NameError source removed


def test_finding_lifecycle_and_illegal_rejected():
    reg = _Reg([_job()])
    mgr = ReconciliationManager(reg, None, _Cleanup())
    found = mgr.report_event({"event_type": "EPOCH_CONFLICT", "job_id": "j1",
                              "execution_epoch": 0})
    assert len(found) == 1
    fid = found[0].finding_id
    # REQUEST_REMEDIATION path advances OPEN→ACK→REMEDIATING during processing.
    assert mgr.get_finding(fid).status == FindingStatus.REMEDIATING
    mgr.resolve_finding(fid)
    assert mgr.get_finding(fid).status == FindingStatus.RESOLVED
    with pytest.raises(RuntimeError):
        mgr.transition_finding(fid, FindingStatus.OPEN)


def test_ownership_gate_blocks_job():
    reg = _Reg([_job(state="MIGRATING", active="m1")])
    mgr = ReconciliationManager(reg, None, _Cleanup())
    mgr.report_event({"event_type": "SPLIT_BRAIN", "job_id": "j1",
                      "migration_id": "m1", "execution_epoch": 1})
    assert ("j1", "RECONCILIATION_REQUIRED") in reg.gated


def test_sweep_finds_dangling_stale_and_infra_mismatch():
    reg = _Reg([
        _job("j1", state="MIGRATING", active=None, inst="i-1"),   # dangling
        _job("j2", state="RUNNING", active="m-old", inst="i-2"),  # stale
        _job("j3", state="MIGRATING", active="m3", inst="i-3"),   # infra gone
    ])
    infra = _Infra({"i-1": "RUNNING", "i-2": "RUNNING", "i-3": "TERMINATED"})
    mgr = ReconciliationManager(reg, infra, _Cleanup())
    found = mgr.reconcile(ReconciliationTrigger.PERIODIC_SWEEP)
    kinds = {(f.job_id, f.mismatch_type) for f in found}
    assert ("j1", MismatchType.DANGLING_MIGRATION_REFERENCE) in kinds
    assert ("j2", MismatchType.STALE_MIGRATION) in kinds
    assert ("j3", MismatchType.REGISTRY_INFRASTRUCTURE_MISMATCH) in kinds


def test_preauthorized_cleanup_resolves_directly():
    reg = _Reg([_job()])
    cleanup = _Cleanup()
    mgr = ReconciliationManager(reg, None, cleanup)
    finding = mgr._new_finding(
        job_id="j1", migration_id=None, mismatch_type=MismatchType.ORPHAN_CHECKPOINT,
        trigger=ReconciliationTrigger.PERIODIC_SWEEP,
        details={"resource_id": "chk-tmp", "resource_type": "checkpoint"},
        remediation={"type": "DELETE_TEMPORARY", "reason": "t", "pre_authorized": True})
    mgr._findings[finding.finding_id] = finding
    mgr._process_finding(finding)
    assert cleanup.calls == [("chk-tmp", "checkpoint")]
    assert mgr.get_finding(finding.finding_id).status == FindingStatus.RESOLVED


def test_general_remediation_routes_via_callback():
    reg = _Reg([_job()])
    seen = []
    mgr = ReconciliationManager(reg, None, _Cleanup(),
                                remediation_callback=seen.append)
    mgr.report_event({"event_type": "TARGET_LOST", "job_id": "j1",
                      "migration_id": "m9"})
    assert seen and seen[0].finding_id
    assert seen[0].action_type == MismatchType.TARGET_MISSING.value
