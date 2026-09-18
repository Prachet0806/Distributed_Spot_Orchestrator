"""Track B2/B3: CPR lease self-fencing + §14.3 bootstrap + plan-step CAS.

Vehicles: S09/S10-adjacent crash-recovery evidence (emulated), IA-1
(crash ≠ workload failure) unit evidence, C-PLAN step CAS.
"""
from datetime import datetime, timezone, timedelta

import pytest

from orchestrator.bootstrap import bootstrap_recovery
from orchestrator.control_plane_lease import (
    ControlPlaneLease, LeaseStore,
)
from storage.plan_store import PlanStore, PlanStepConflictError


class _Clock:
    def __init__(self, start=1_000_000.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class _FakeTable:
    """Captures Dynamo write shapes; emulates conditional-check failures."""

    def __init__(self, fail_conditions=False):
        self.puts = []
        self.updates = []
        self.items = {}
        self.fail_conditions = fail_conditions

    def put_item(self, **kw):
        self.puts.append(kw)
        if self.fail_conditions and "ConditionExpression" in kw:
            raise RuntimeError("ConditionalCheckFailedException")
        item = dict(kw["Item"])
        key = item.get("plan_id", item.get("lease_id"))
        self.items[key] = item

    def update_item(self, **kw):
        self.updates.append(kw)
        if self.fail_conditions and "ConditionExpression" in kw:
            raise RuntimeError("ConditionalCheckFailedException")
        return {}

    def get_item(self, **kw):
        key = kw["Key"].get("plan_id", kw["Key"].get("lease_id"))
        if key in self.items:
            return {"Item": dict(self.items[key])}
        return {}


class _FakeDynamo:
    def __init__(self, **tables):
        self.tables = tables

    def Table(self, name):
        return self.tables[name]


# -- lease --
def test_lease_first_wins_and_heartbeat_renews():
    clock = _Clock()
    store = LeaseStore()
    a = ControlPlaneLease(store, owner="cpr-a", wall_clock=clock)
    b = ControlPlaneLease(store, owner="cpr-b", wall_clock=clock)
    assert a.acquire() and not b.acquire()
    assert a.is_holder() and not b.is_holder()
    clock.advance(15.0)
    assert a.heartbeat_due() and a.heartbeat()
    assert store.get("cpr-primary")["version"] == 2
    # Stale-version renew (simulated rival writer) is refused.
    assert store.renew("cpr-primary", "cpr-a", 1, "2099-01-01") is False
    a.release()
    assert b.acquire() and b.is_holder()


def test_lost_lease_fences_holder():
    clock = _Clock()
    store = LeaseStore()
    a = ControlPlaneLease(store, owner="cpr-a", wall_clock=clock)
    assert a.acquire()
    # Rival steals the record out-of-band (e.g. TTL expiry + re-acquire).
    store._leases["cpr-primary"]["owner"] = "cpr-b"
    assert a.heartbeat() is False and not a.is_holder()


def test_lease_dynamo_uses_conditional_writes():
    table = _FakeTable()
    store = LeaseStore(dynamodb_resource=_FakeDynamo(
        spot_arbitrage_control_plane_lease=table))
    assert store.acquire("cpr-primary", "cpr-a", "2099-01-01") is True
    assert "ConditionExpression" in table.puts[0]
    assert store.renew("cpr-primary", "cpr-a", 1, "2099-01-01") is True
    cond = table.updates[0]["ConditionExpression"]
    assert "version" in cond and "owner" in cond


# -- plan-step CAS --
def test_plan_step_advance_bumps_version_and_guards_hash():
    store = PlanStore()
    from orchestrator.models_v2 import MigrationPlanV2
    plan = MigrationPlanV2(plan_id="p1", job_id="j", migration_id="m",
                           regime="ARBITRAGE", source_pool_id="s",
                           target_pool_id="t", steps=[])
    doc = store.put_plan(plan)
    assert doc["version"] == 0
    # Local step advance path works on typed docs with steps; emulate one.
    store._plans["p1"]["steps"] = [
        {"step_id": "a", "type": "CHECKPOINT", "state": "PENDING"}]
    out = store.update_step_state("p1", "a", "PENDING", "RUNNING",
                                  operation_id="op-1")
    assert out["state"] == "RUNNING"
    assert store._plans["p1"]["version"] == 1


def test_plan_step_dynamo_cas_conditions_on_hash_and_version():
    table = _FakeTable()
    store = PlanStore(dynamodb_resource=_FakeDynamo(
        spot_arbitrage_plans=table))
    store._plans["p1"] = {
        "plan_id": "p1", "plan_hash": "h1", "version": 3,
        "steps": [{"step_id": "a", "type": "CHECKPOINT", "state": "RUNNING"}]}
    with pytest.raises(PlanStepConflictError):
        store.update_step_state("p1", "a", "PENDING", "SUCCEEDED")
    assert table.updates == []  # refused before any write
    store.update_step_state("p1", "a", "RUNNING", "SUCCEEDED")
    cond = table.updates[0]["ConditionExpression"]
    assert "plan_hash" in cond and "version" in cond


# -- bootstrap --
def _doc(plan_id="plan-1", migration="m-1", job="job-1", steps=(),
         deadline=None, expired=False):
    return {
        "plan_id": plan_id, "migration_id": migration, "job_id": job,
        "plan_hash": "h", "version": 1,
        "absolute_deadline": deadline,
        "expires_at": expired,
        "steps": list(steps),
    }


def _step(sid, stype, state="PENDING", op=None):
    return {"step_id": sid, "type": stype, "state": state,
            "operation_id": op}


class _MemPlanStore:
    def __init__(self, docs):
        self._docs = {d["plan_id"]: d for d in docs}

    def list_active(self):
        return [d for d in self._docs.values()
                if any(str(s.get("state", "")).upper()
                       not in ("SUCCEEDED", "FAILED", "SKIPPED")
                       for s in d.get("steps", []))]


class _Lease:
    def __init__(self, held=True):
        self._held = held
        self.beats = 0

    def is_holder(self):
        return self._held

    def acquire(self):
        return self._held

    def heartbeat(self):
        self.beats += 1
        return self._held


NOW = datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc)


def test_bootstrap_halts_without_lease():
    store = _MemPlanStore([_doc(steps=[_step("s", "CHECKPOINT")])])
    rep = bootstrap_recovery(lease=_Lease(held=False), plan_store=store,
                             now=NOW)
    assert rep["halted"] and rep["resumed"] == []


def test_bootstrap_resumes_at_frontier_with_budget():
    doc = _doc(deadline=(NOW + timedelta(minutes=10)).isoformat(), steps=[
        _step("pre", "PRECHECK", "SUCCEEDED"),
        _step("chk", "CHECKPOINT", "SUCCEEDED"),
        _step("prov", "PROVISION", "RUNNING", op="op-1")])
    rep = bootstrap_recovery(lease=_Lease(), plan_store=_MemPlanStore([doc]),
                             now=NOW)
    assert not rep["halted"]
    assert rep["resumed"] == [{
        "plan_id": "plan-1", "migration_id": "m-1", "job_id": "job-1",
        "resume_from_step": "prov", "resume_from_type": "PROVISION",
        "forward_only": False,
        "remaining_budget_seconds": pytest.approx(600.0)}]


def test_bootstrap_post_fence_frontier_is_forward_only():
    doc = _doc(deadline=(NOW + timedelta(minutes=10)).isoformat(), steps=[
        _step("r", "RESTORE", "SUCCEEDED"),
        _step("f", "FENCE", "SUCCEEDED"),
        _step("v", "VALIDATE", "RUNNING", op="op-v")])
    rep = bootstrap_recovery(lease=_Lease(), plan_store=_MemPlanStore([doc]),
                             now=NOW)
    assert rep["resumed"][0]["resume_from_type"] == "VALIDATE"
    assert rep["resumed"][0]["forward_only"] is True


def test_bootstrap_resolves_unknown_and_gates_unresolved():
    doc = _doc(deadline=(NOW + timedelta(minutes=10)).isoformat(), steps=[
        _step("p", "PROVISION", "UNKNOWN", op="op-ok"),
        _step("t", "TRANSFER", "PENDING")])
    doc2 = _doc(plan_id="plan-2", migration="m-2", steps=[
        _step("c", "CHECKPOINT", "UNKNOWN", op="op-lost")])

    def resolve(op_id):
        return "SUCCEEDED" if op_id == "op-ok" else "UNKNOWN"

    rep = bootstrap_recovery(
        lease=_Lease(), plan_store=_MemPlanStore([doc, doc2]),
        resolve_operation=resolve, now=NOW)
    assert rep["unknown_resolved"] and rep["unknown_resolved"][0]["operation_id"] == "op-ok"
    assert rep["unknown_unresolved"] and rep["unknown_unresolved"][0]["operation_id"] == "op-lost"
    # Resolved plan resumes; unresolved plan waits for reconciliation.
    assert [r["plan_id"] for r in rep["resumed"]] == ["plan-1"]


def test_bootstrap_abandons_expired_and_reports_orphans():
    doc = _doc(deadline=(NOW - timedelta(seconds=1)).isoformat(), steps=[
        _step("c", "CHECKPOINT", "RUNNING")])
    rep = bootstrap_recovery(
        lease=_Lease(), plan_store=_MemPlanStore([doc]),
        active_refs=[{"job_id": "job-9", "migration_id": "m-ghost",
                      "execution_epoch": 3}],
        now=NOW)
    assert rep["abandoned"] and rep["abandoned"][0]["reason"] == "DEADLINE_EXCEEDED"
    assert rep["resumed"] == []
    assert rep["orphans"] == [{"job_id": "job-9", "migration_id": "m-ghost",
                               "execution_epoch": 3}]
