# orchestrator/bootstrap.py — CPR crash-recovery bootstrap (§14.3, Track B3).
"""Restart procedure after a control-plane crash or failover:

lease → in-flight → UNKNOWN resolution → budget → frontier → sweep → resume.

Pure over injected collaborators (no AWS calls): the caller supplies the
lease, plan store, active registry refs, an operation-status resolver, and
a clock. The report NEVER actuates — it tells the main loop what is safe
to resume, what needs reconciliation, and what must halt. Wall-clock
`absolute_deadline` is the conservative budget basis (monotonic anchors do
not survive restart, Protocols §24.1).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

# Canonical step order; the frontier is the furthest non-terminal step.
STEP_ORDER = ["PRECHECK", "CHECKPOINT", "PERSIST", "PROVISION", "TRANSFER",
              "RESTORE", "FENCE", "VALIDATE", "ACTIVATE", "FINALIZE"]
TERMINAL_STEP_STATES = {"SUCCEEDED", "FAILED", "SKIPPED"}
# Post-fencing frontier: never roll back, forward recovery only (I4).
POST_FENCE_STEPS = {"FENCING", "FENCE", "VALIDATING", "VALIDATE",
                    "ACTIVATING", "ACTIVATE", "FINALIZING", "FINALIZE"}


def _parse_time(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        ts = value
    elif isinstance(value, str) and value:
        try:
            ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _frontier(steps: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Furthest started step not in a terminal state (plan order).

    Started (RUNNING/UNKNOWN) outranks merely PENDING: trailing PENDING
    steps after a crash point must not masquerade as the frontier (a plan
    that died in TRANSFER resumes from TRANSFER, not FINALIZE). An
    all-PENDING plan resumes from its first step.
    """
    order = {name: i for i, name in enumerate(STEP_ORDER)}
    pending = [s for s in steps
               if str(s.get("state", "")).upper() not in TERMINAL_STEP_STATES]
    if not pending:
        return None

    def _rank(step: Dict[str, Any]) -> int:
        stype = str(step.get("type", "")).upper()
        return order.get(stype, len(order))

    started = [s for s in pending
               if str(s.get("state", "")).upper() in ("RUNNING", "UNKNOWN")]
    candidates = started or pending
    # Furthest = max rank; ties keep document order (stable max).
    best, best_rank = None, -1
    for step in candidates:
        rank = _rank(step)
        if rank >= best_rank:
            best, best_rank = step, rank
    return best


def bootstrap_recovery(
    *,
    lease: Any,
    plan_store: Any,
    active_refs: Optional[List[Dict[str, Any]]] = None,
    resolve_operation: Optional[Callable[[str], str]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Run the §14.3 bootstrap. Returns a resume/reconcile/halt report."""
    now = now or datetime.now(timezone.utc)
    report: Dict[str, Any] = {
        "halted": False, "halt_reason": None,
        "resumed": [], "abandoned": [],
        "unknown_unresolved": [], "unknown_resolved": [],
        "orphans": [],
    }

    # 1. Lease: no fresh holder ⇒ halt, never actuate.
    try:
        held = lease.acquire() if not lease.is_holder() else lease.heartbeat()
    except Exception as exc:
        held = False
        report["halt_reason"] = f"lease error: {exc}"
    if not held:
        report["halted"] = True
        report["halt_reason"] = report["halt_reason"] or "lease not held"
        return report

    # 2. In-flight: non-terminal plan-step states + registry active refs.
    try:
        in_flight = {doc.get("plan_id"): doc for doc in plan_store.list_active()}
    except Exception as exc:
        report["halted"] = True
        report["halt_reason"] = f"plan store unreadable: {exc}"
        return report

    known_migrations = {doc.get("migration_id") for doc in in_flight.values()}

    # 3. UNKNOWN resolution: never blind-retry; unresolved ⇒ reconciliation.
    for plan_id, doc in in_flight.items():
        for step in doc.get("steps", []) or []:
            if str(step.get("state", "")).upper() != "UNKNOWN":
                continue
            op_id = step.get("operation_id")
            outcome = "UNKNOWN"
            if op_id and resolve_operation is not None:
                try:
                    outcome = str(resolve_operation(op_id)).upper()
                except Exception:
                    outcome = "UNKNOWN"
            entry = {"plan_id": plan_id,
                     "migration_id": doc.get("migration_id"),
                     "job_id": doc.get("job_id"),
                     "step_id": step.get("step_id"),
                     "step_type": str(step.get("type", "")).upper(),
                     "operation_id": op_id,
                     "outcome": outcome}
            if outcome in ("SUCCEEDED", "FAILED"):
                report["unknown_resolved"].append(entry)
            else:
                report["unknown_unresolved"].append(entry)

    unresolved_plans = {e["plan_id"] for e in report["unknown_unresolved"]}

    # 4-5. Budget (wall-clock) → frontier → resume/abandon per plan.
    for plan_id, doc in in_flight.items():
        if plan_id in unresolved_plans:
            continue  # ownership/outcome uncertain → reconciliation owns it
        remaining = None
        deadline = _parse_time(doc.get("absolute_deadline"))
        if deadline is not None:
            remaining = (deadline - now).total_seconds()
        elif doc.get("expires_at"):
            expiry = _parse_time(doc.get("expires_at"))
            if expiry is not None:
                remaining = (expiry - now).total_seconds()
        frontier = _frontier(doc.get("steps", []) or [])
        if frontier is None:
            continue
        ftype = str(frontier.get("type", "")).upper()
        forward = ftype in POST_FENCE_STEPS
        if remaining is not None and remaining <= 0 and not forward:
            # Pre-fence expiry abandons to recovery. Post-fence expiry
            # resumes forward-only (COMPLETE_FENCING, I10): a breached
            # deadline never interrupts an in-progress ownership
            # transition — matching the live Coordinator, which continues
            # post-fence instead of aborting.
            report["abandoned"].append({
                "plan_id": plan_id, "migration_id": doc.get("migration_id"),
                "job_id": doc.get("job_id"),
                "reason": "DEADLINE_EXCEEDED"})
            continue
        report["resumed"].append({
            "plan_id": plan_id,
            "migration_id": doc.get("migration_id"),
            "job_id": doc.get("job_id"),
            "resume_from_step": frontier.get("step_id"),
            "resume_from_type": ftype,
            "forward_only": forward,
            "remaining_budget_seconds": remaining,
        })

    # 6. Sweep: registry refs with no plan record ⇒ orphan findings input.
    for ref in active_refs or []:
        mid = (ref.get("migration_id") if isinstance(ref, dict) else None)
        if mid and mid not in known_migrations:
            report["orphans"].append(ref)

    return report


def collect_active_refs(registry: Any,
                        states: tuple = ("MIGRATING",
                                         "RECONCILIATION_REQUIRED")) -> List[Dict[str, Any]]:
    """Gather registry active-migration refs for the orphan sweep (§14.3.6).

    Never raises: an unreadable registry yields no refs (the bootstrap
    then treats every in-flight plan as unowned-by-registry, which the
    sweep surfaces rather than hides).
    """
    refs: List[Dict[str, Any]] = []
    list_by_state = getattr(registry, "list_by_state", None)
    if not callable(list_by_state):
        return refs
    for state in states:
        try:
            jobs = list_by_state(state) or []
        except Exception:
            continue
        for job in jobs:
            if not isinstance(job, dict):
                continue
            if job.get("active_migration_id"):
                refs.append({
                    "job_id": job.get("job_id"),
                    "migration_id": job.get("active_migration_id"),
                    "execution_epoch": int(job.get("execution_epoch", 0) or 0),
                })
    return refs


def fence_adjacent_unresolved(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Unknown-outcome entries at/after the fence frontier (S10 triage).

    These must gate on reconciliation (never inferred, never resumed):
    ownership may already have moved. Pre-fence unknowns are left to the
    periodic sweep (stale/dangling findings), which already covers them.
    """
    return [e for e in report.get("unknown_unresolved", [])
            if str(e.get("step_type", "")).upper() in POST_FENCE_STEPS]
