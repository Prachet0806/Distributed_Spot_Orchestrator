# orchestrator/replan.py — replan / supersession policy helpers (§25, Track C5).
"""Pure helpers for the Recovery → Planner → Coordinator replan path.

Authority boundaries (Protocols §25):

- A new plan revision NEVER implies a new migration attempt: successors
  keep `migration_id` and link via `supersedes_plan_id`. Only the
  execution attempt changing requires a new `migration_id`.
- Replanning is pre-fence only. Post-fencing outcomes are forward
  recovery (RETRY_RESTORE / FORWARD_RECOVERY / COLD_RESTART / FAILED) —
  never a new plan against the fenced source.
- A fresh `workload_estimate` is required for every successor (enforced
  by `MigrationPlanner.create_successor_plan`): replanning on stale
  evidence is prohibited.
- The main loop owns attempt budgets (`max_replans`, baseline
  `retry.max_replans`, default 2) via the in-process `replan_counts`
  map. Durable cross-restart budget (plan-chain walk) rides the Plan
  Store wiring (Track B1); this map covers single-process duplicates.

S16 note: `preempt_with_emergency()` builds the emergency successor for
an in-flight pre-fence arbitrage plan. Marking the old plan SUPERSEDED
is the caller's job — `Coordinator.supersede_and_replan()` for live
executions, plan-store/registry transitions for stored plans (Sprint 4).
The main-loop pre-emption trigger additionally needs plan persistence
(Track B1: plans are not yet admitted to the Plan Store in `main.py`).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Optional

from orchestrator.migration_coordinator import MigrationExecutionState
from orchestrator.migration_planner import MigrationPlan, MigrationState
from orchestrator.policy_engine import MigrationRegime

logger = logging.getLogger(__name__)

DEFAULT_MAX_REPLANS = 2


def max_replans_from_baseline(baseline: Optional[dict]) -> int:
    """Read the attempt budget from the frozen baseline (ADR-012)."""
    try:
        return int((baseline or {}).get("retry", {}).get(
            "max_replans", DEFAULT_MAX_REPLANS))
    except (TypeError, ValueError, AttributeError):
        return DEFAULT_MAX_REPLANS


def budget_remaining(replan_counts: Dict[str, int], migration_id: str,
                     max_replans: int = DEFAULT_MAX_REPLANS) -> bool:
    """True when another successor attempt may be issued for this attempt."""
    return int(replan_counts.get(migration_id, 0)) < int(max_replans)


def note_replan(replan_counts: Dict[str, int], migration_id: str) -> int:
    """Record an issued successor; returns the new count (1-based)."""
    replan_counts[migration_id] = int(replan_counts.get(migration_id, 0)) + 1
    return replan_counts[migration_id]


def replan_signal_for(
    exec_state: Optional[MigrationExecutionState],
    recovery_replan_required: bool = False,
) -> Optional[str]:
    """Decide whether a terminal execution warrants a successor plan.

    Returns the replan reason, or None when the outcome must stand.
    Post-fence outcomes (fencing_confirmed) NEVER replan — forward
    recovery owns them. Non-terminal and non-failed/aborted states carry
    no signal. Recovery Policy `replan_required` is honored pre-fence.
    """
    if exec_state is None:
        return None
    if exec_state.current_state not in (MigrationState.FAILED,
                                        MigrationState.ABORTED):
        return None
    if exec_state.fencing_confirmed:
        return None  # forward-only; a new plan cannot unfence the source
    if exec_state.replan_requested:
        return exec_state.replan_requested
    if recovery_replan_required:
        return "recovery-requested-replan"
    return None


def preempt_with_emergency(
    planner: Any,
    old_plan: MigrationPlan,
    *,
    workload_estimate: Any,
    policy_decision: Any = None,
    recovery_feasibility: Any = None,
    absolute_deadline: Optional[datetime] = None,
    target_pool_id: Optional[str] = None,
    fencing_confirmed: bool = False,
) -> MigrationPlan:
    """Build the S16 emergency successor for an in-flight arbitrage plan.

    Same `migration_id` (same attempt), `regime=EMERGENCY`, fresh estimate
    and feasibility, linked via `supersedes_plan_id`. Refuses loudly when
    fencing already confirmed — pre-empting post-fence would fork
    ownership (forward recovery is the only legal path there).
    """
    if fencing_confirmed:
        raise ValueError(
            "refusing emergency pre-emption post-fencing: forward recovery "
            "only (§25, Protocols #4 §6.3)")
    if old_plan.regime == MigrationRegime.EMERGENCY:
        raise ValueError(
            "old plan is already EMERGENCY: nothing to pre-empt")
    overrides: Dict[str, Any] = {
        "regime": MigrationRegime.EMERGENCY,
    }
    if absolute_deadline is not None:
        overrides["absolute_deadline"] = absolute_deadline
    if recovery_feasibility is not None:
        overrides["recovery_feasibility"] = recovery_feasibility
    if target_pool_id is not None:
        overrides["target_pool_id"] = target_pool_id
    return planner.create_successor_plan(
        old_plan, "emergency-preempts",
        workload_estimate=workload_estimate,
        policy_decision=policy_decision or old_plan.policy_decision,
        **overrides,
    )
