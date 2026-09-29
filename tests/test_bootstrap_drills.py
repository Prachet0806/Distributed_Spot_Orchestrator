"""Sprint 4 (Track B3/B4): CPR crash-matrix drills over §14.3 bootstrap.

Vehicles: S09 (crash at every migration state → resume, RPO 0), S10
(mid-FENCING crash → matrix outcome, never inferred), S17 (clock expiry
mid-FENCING → safe forward conclusion), IA-1 (crash ≠ workload failure)
unit evidence. All emulated: no AWS, no actuation (report-only).
"""
from datetime import datetime, timedelta, timezone

import pytest

from orchestrator.bootstrap import (
    POST_FENCE_STEPS,
    STEP_ORDER,
    bootstrap_recovery,
    collect_active_refs,
    fence_adjacent_unresolved,
)
from orchestrator.control_plane_lease import (
    ControlPlaneLease,
    LeaseStore,
    check_lease_for_iteration,
)

NOW = datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc)
FUTURE = (NOW + timedelta(minutes=10)).isoformat()
PAST = (NOW - timedelta(seconds=1)).isoformat()


class _Clock:
    def __init__(self, start=1_000_000.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class _Lease:
    def __init__(self, held=True):
        self._held = held

    def is_holder(self):
        return self._held

    def acquire(self):
        return self._held

    def heartbeat(self):
        return self._held

    def heartbeat_due(self):
        return True


class _MemPlanStore:
    def __init__(self, docs):
        self._docs = {d["plan_id"]: d for d in docs}

    def list_active(self):
        return [d for d in self._docs.values()
                if any(str(s.get("state", "")).upper()
                       not in ("SUCCEEDED", "FAILED", "SKIPPED")
                       for s in d.get("steps", []))]


def _step(sid, stype, state="PENDING", op=None):
    return {"step_id": sid, "type": stype, "state": state,
            "operation_id": op}


def _crashed_at(frontier_type, frontier_state="RUNNING", deadline=FUTURE,
                plan_id="plan-1", unknown_op=None):
    """Plan doc as if CPR died with the frontier at `frontier_type`.

    Lists steps up to the frontier only (store convention: unreached
    steps are absent, not PENDING).
    """
    steps = []
    for name in STEP_ORDER:
        if name == frontier_type:
            state = ("UNKNOWN" if unknown_op else frontier_state)
            steps.append(_step(f"s-{name.lower()}", name, state,
                               op=unknown_op if unknown_op else None))
            break
        steps.append(_step(f"s-{name.lower()}", name, "SUCCEEDED"))
    return {"plan_id": plan_id, "migration_id": "m-1", "job_id": "job-1",
            "plan_hash": "h", "version": 1, "absolute_deadline": deadline,
            "steps": steps}


# -- S09: crash at every migration state resumes at the frontier --
@pytest.mark.parametrize("step_type", STEP_ORDER)
def test_s09_crash_at_every_state_resumes_at_frontier(step_type):
    rep = bootstrap_recovery(
        lease=_Lease(), plan_store=_MemPlanStore([_crashed_at(step_type)]),
        now=NOW)
    assert not rep["halted"]
    assert len(rep["resumed"]) == 1
    resumed = rep["resumed"][0]
    assert resumed["resume_from_type"] == step_type
    assert resumed["forward_only"] == (step_type in POST_FENCE_STEPS)
    assert resumed["remaining_budget_seconds"] == pytest.approx(600.0)
    assert rep["abandoned"] == [] and rep["unknown_unresolved"] == []


# -- S09/UNKNOWN: resolved outcomes resume; unresolved are held --
@pytest.mark.parametrize("step_type", STEP_ORDER)
def test_unknown_resolved_at_every_state_resumes(step_type):
    doc = _crashed_at(step_type, unknown_op="op-9")
    rep = bootstrap_recovery(
        lease=_Lease(), plan_store=_MemPlanStore([doc]),
        resolve_operation=lambda op: "SUCCEEDED", now=NOW)
    assert rep["unknown_resolved"] and rep["unknown_resolved"][0]["operation_id"] == "op-9"
    assert rep["unknown_resolved"][0]["step_type"] == step_type
    assert [r["plan_id"] for r in rep["resumed"]] == ["plan-1"]
    assert rep["unknown_unresolved"] == []


@pytest.mark.parametrize("step_type", STEP_ORDER)
def test_unknown_unresolved_at_any_state_is_held(step_type):
    doc = _crashed_at(step_type, unknown_op="op-lost")
    rep = bootstrap_recovery(
        lease=_Lease(), plan_store=_MemPlanStore([doc]),
        resolve_operation=lambda op: "UNKNOWN", now=NOW)
    assert rep["resumed"] == []  # never inferred, never resumed
    assert len(rep["unknown_unresolved"]) == 1
    entry = rep["unknown_unresolved"][0]
    assert entry["step_type"] == step_type
    assert entry["job_id"] == "job-1"


# -- S17: clock expiry mid-FENCING concludes forward, never aborts --
@pytest.mark.parametrize("step_type", ["FENCE", "VALIDATE", "ACTIVATE", "FINALIZE"])
def test_s17_expired_post_fence_frontier_resumes_forward_only(step_type):
    rep = bootstrap_recovery(
        lease=_Lease(),
        plan_store=_MemPlanStore([_crashed_at(step_type, deadline=PAST)]),
        now=NOW)
    assert rep["abandoned"] == []  # COMPLETE_FENCING: deadline never interrupts
    assert len(rep["resumed"]) == 1
    assert rep["resumed"][0]["forward_only"] is True
    assert rep["resumed"][0]["remaining_budget_seconds"] < 0


@pytest.mark.parametrize("alias", ["FENCING", "VALIDATING", "ACTIVATING",
                                   "FINALIZING"])
def test_state_name_aliases_classify_forward_only(alias):
    """MigrationState names (vs PlanStepType names) are fence-adjacent too."""
    doc = _crashed_at("TRANSFER", deadline=PAST)
    doc["steps"].append(_step("s-alias", alias, "RUNNING"))
    rep = bootstrap_recovery(
        lease=_Lease(), plan_store=_MemPlanStore([doc]), now=NOW)
    assert rep["abandoned"] == []
    assert rep["resumed"][0]["forward_only"] is True


def test_expired_pre_fence_frontier_abandons():
    rep = bootstrap_recovery(
        lease=_Lease(),
        plan_store=_MemPlanStore([_crashed_at("TRANSFER", deadline=PAST)]),
        now=NOW)
    assert rep["resumed"] == []
    assert rep["abandoned"][0]["reason"] == "DEADLINE_EXCEEDED"
    assert rep["abandoned"][0]["job_id"] == "job-1"


# -- S10: mid-FENCING triage gates fence-adjacent unknowns only --
def test_s10_fence_unknown_gates_while_transfer_unknown_sweeps():
    fence_doc = _crashed_at("PROVISION", unknown_op="op-fence")
    # Rewrite the unknown onto the FENCE step with predecessors succeeded.
    fence_doc["steps"] = [
        _step("s-precheck", "PRECHECK", "SUCCEEDED"),
        _step("s-checkpoint", "CHECKPOINT", "SUCCEEDED"),
        _step("s-persist", "PERSIST", "SUCCEEDED"),
        _step("s-provision", "PROVISION", "SUCCEEDED"),
        _step("s-transfer", "TRANSFER", "SUCCEEDED"),
        _step("s-restore", "RESTORE", "SUCCEEDED"),
        _step("s-fence", "FENCE", "UNKNOWN", op="op-fence"),
        _step("s-validate", "VALIDATE", "PENDING"),
    ]
    transfer_doc = _crashed_at("TRANSFER", unknown_op="op-xfer",
                               plan_id="plan-2")
    rep = bootstrap_recovery(
        lease=_Lease(), plan_store=_MemPlanStore([fence_doc, transfer_doc]),
        resolve_operation=lambda op: "UNKNOWN", now=NOW)
    assert rep["resumed"] == []
    gated = fence_adjacent_unresolved(rep)
    assert [e["operation_id"] for e in gated] == ["op-fence"]
    assert gated[0]["step_type"] == "FENCE"


# -- lease guard --
def test_lease_guard_continues_for_fresh_holder():
    clock = _Clock()
    lease = ControlPlaneLease(LeaseStore(), owner="cpr-a", wall_clock=clock)
    assert lease.acquire()
    ok, action = check_lease_for_iteration(lease)
    assert (ok, action) == (True, "continue")


def test_lease_guard_heartbeats_when_due_and_continues():
    clock = _Clock()
    lease = ControlPlaneLease(LeaseStore(), owner="cpr-a", wall_clock=clock)
    assert lease.acquire()
    clock.advance(15.0)
    ok, action = check_lease_for_iteration(lease)
    assert (ok, action) == (True, "continue")


def test_lease_guard_fences_on_rival_takeover():
    clock = _Clock()
    store = LeaseStore()
    lease = ControlPlaneLease(store, owner="cpr-a", wall_clock=clock)
    assert lease.acquire()
    store._leases["cpr-primary"]["owner"] = "cpr-b"  # TTL expiry + rival
    clock.advance(15.0)
    ok, action = check_lease_for_iteration(lease)
    assert (ok, action) == (False, "fenced")


def test_lease_guard_halts_for_non_holder():
    ok, action = check_lease_for_iteration(_Lease(held=False))
    assert (ok, action) == (False, "not-holder")


def test_lease_guard_reports_lease_errors():
    class _Boom:
        def is_holder(self):
            raise RuntimeError("store gone")
    ok, action = check_lease_for_iteration(_Boom())
    assert ok is False and action.startswith("lease-error")


# -- active-ref collection --
def test_collect_active_refs_from_registry_states():
    class _Reg:
        def list_by_state(self, state):
            if state == "MIGRATING":
                return [{"job_id": "j1", "state": "MIGRATING",
                         "execution_epoch": 3, "active_migration_id": "m-1"},
                        {"job_id": "j2", "state": "MIGRATING",
                         "execution_epoch": 0, "active_migration_id": None}]
            if state == "RECONCILIATION_REQUIRED":
                return [{"job_id": "j3", "state": "RECONCILIATION_REQUIRED",
                         "execution_epoch": 1, "active_migration_id": "m-3"}]
            raise AssertionError("unexpected state " + state)

    refs = collect_active_refs(_Reg())
    assert refs == [
        {"job_id": "j1", "migration_id": "m-1", "execution_epoch": 3},
        {"job_id": "j3", "migration_id": "m-3", "execution_epoch": 1},
    ]


def test_collect_active_refs_never_raises():
    assert collect_active_refs(object()) == []

    class _Bad:
        def list_by_state(self, state):
            raise RuntimeError("dynamo down")

    assert collect_active_refs(_Bad()) == []
