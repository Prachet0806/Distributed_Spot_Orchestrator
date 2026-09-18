# orchestrator/pool_lifecycle.py — pool lifecycle + per-pool concurrency (Track C3).
"""Candidate-pool lifecycle (Protocols §7.4.2) and concurrency double-guard.

Lifecycle: ACTIVE → DEGRADED → RETIRING → RETIRED (DEGRADED may recover to
ACTIVE; RETIRED is terminal). Eligibility: ACTIVE always; DEGRADED only for
non-emergency; RETIRING/RETIRED never.

Concurrency: each pool declares `max_concurrent_migrations`. Admission of a
new migration targeting a pool requires
`active_migrations_targeting(pool) < max`; exceeding yields
CAPACITY_UNAVAILABLE, never queueing. The guard is doubled: Placement peeks
(admission-time exclusion) and the Coordinator acquires (execution-time
refusal) — either layer independently refuses an over-subscribed pool.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from enum import Enum
from typing import Any, Dict, Iterator, Optional


class PoolLifecycle(str, Enum):
    ACTIVE = "ACTIVE"
    DEGRADED = "DEGRADED"
    RETIRING = "RETIRING"
    RETIRED = "RETIRED"


LEGAL_TRANSITIONS: dict[str, frozenset] = {
    "ACTIVE": frozenset({"DEGRADED", "RETIRING"}),
    "DEGRADED": frozenset({"ACTIVE", "RETIRING"}),
    "RETIRING": frozenset({"RETIRED"}),
    "RETIRED": frozenset(),
}

# v2_baseline.yaml → concurrency.max_concurrent_migrations_per_pool
DEFAULT_MAX_CONCURRENT_MIGRATIONS_PER_POOL = 2


class IllegalLifecycleTransition(ValueError):
    pass


def normalize_lifecycle(value: Any) -> str:
    name = getattr(value, "name", value)
    text = str(name if name is not None else "ACTIVE").upper()
    if "." in text:
        text = text.rsplit(".", 1)[1]
    if text not in LEGAL_TRANSITIONS:
        raise ValueError(f"unknown pool lifecycle: {value!r}")
    return text


def transition_lifecycle(current: Any, target: Any) -> str:
    """Legal lifecycle advance; raises IllegalLifecycleTransition otherwise."""
    cur, tgt = normalize_lifecycle(current), normalize_lifecycle(target)
    if tgt != cur and tgt not in LEGAL_TRANSITIONS[cur]:
        raise IllegalLifecycleTransition(
            f"illegal pool lifecycle transition {cur} → {tgt}")
    return tgt


def eligible_for_regime(lifecycle: Any, regime: Any) -> bool:
    """Pool-lifecycle eligibility gate (independent of compat/readiness).

    `regime` is a MigrationRegime or its value string; EMERGENCY excludes
    DEGRADED pools (Protocols §7.4.2).
    """
    life = normalize_lifecycle(lifecycle)
    if life in ("RETIRING", "RETIRED"):
        return False
    regime_name = getattr(regime, "name", regime)
    if life == "DEGRADED" and str(regime_name).upper() == "EMERGENCY":
        return False
    return True


class PoolConcurrencyTracker:
    """Thread-safe per-pool active-migration accounting (double-guard).

    Placement calls `would_admit` (peek, no mutation); the Coordinator calls
    `admit` (consume a slot) before provisioning and `release` at every
    terminal outcome. `slot()` bundles acquire/release for scoped use.
    """

    def __init__(self, default_max: int = DEFAULT_MAX_CONCURRENT_MIGRATIONS_PER_POOL):
        self._default_max = default_max
        self._max: Dict[str, int] = {}
        self._active: Dict[str, int] = {}
        self._lock = threading.Lock()

    def configure(self, pool_id: str, max_concurrent: int) -> None:
        with self._lock:
            self._max[pool_id] = max(0, int(max_concurrent))

    def limit_for(self, pool_id: str) -> int:
        with self._lock:
            return self._max.get(pool_id, self._default_max)

    def active_for(self, pool_id: str) -> int:
        with self._lock:
            return self._active.get(pool_id, 0)

    def would_admit(self, pool_id: str) -> bool:
        """Peek: True iff a new migration targeting the pool fits."""
        with self._lock:
            limit = self._max.get(pool_id, self._default_max)
            return self._active.get(pool_id, 0) < limit

    def admit(self, pool_id: str) -> bool:
        """Consume a slot; False (CAPACITY_UNAVAILABLE) when exhausted."""
        with self._lock:
            limit = self._max.get(pool_id, self._default_max)
            if self._active.get(pool_id, 0) >= limit:
                return False
            self._active[pool_id] = self._active.get(pool_id, 0) + 1
            return True

    def release(self, pool_id: Optional[str]) -> None:
        if not pool_id:
            return
        with self._lock:
            if self._active.get(pool_id, 0) > 0:
                self._active[pool_id] -= 1

    @contextmanager
    def slot(self, pool_id: str) -> Iterator[bool]:
        """Yield True with a held slot, else False (no slot consumed)."""
        admitted = self.admit(pool_id)
        try:
            yield admitted
        finally:
            if admitted:
                self.release(pool_id)
