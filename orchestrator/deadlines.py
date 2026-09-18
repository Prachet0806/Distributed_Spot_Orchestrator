# orchestrator/deadlines.py — emergency deadline computation (Protocols §24.1).
"""Computed once at interruption ingestion by the observation lane, carried
immutably on the Migration Plan, enforced by the Coordinator against a
monotonic clock. Never recomputed from fresh timestamps mid-migration."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional


def _norm(region: Optional[str]) -> str:
    return (region or "").strip().lower()


def wan_allowance_seconds(source_region: Optional[str],
                          target_region: Optional[str],
                          home_region: Optional[str],
                          baseline: Optional[dict] = None) -> float:
    """Per-pair WAN allowance with default fallback (§24.1, ADR-011).

    Pairs are unordered (`a/b`); allowances are estimates until Track A6
    replaces them with measured P95 command-leg latencies.
    """
    exec_cfg = (baseline or {}).get("execution", {})
    table = exec_cfg.get("wan_allowance_seconds", {}) or {}
    default = float(table.get("default", 15.0))
    pairs = table.get("pairs", {}) or {}
    a, b = sorted([_norm(source_region), _norm(target_region or source_region)])
    key = f"{a}/{b}"
    # Home-region legs are folded into the pair allowance.
    _ = _norm(home_region)
    return float(pairs.get(key, default))


def compute_absolute_deadline(effective_at: datetime,
                              window_seconds: float,
                              source_region: Optional[str] = None,
                              target_region: Optional[str] = None,
                              home_region: Optional[str] = None,
                              baseline: Optional[dict] = None) -> datetime:
    """absolute_deadline = effective_at + window − wan − skew (§24.1)."""
    exec_cfg = (baseline or {}).get("execution", {})
    skew = float(exec_cfg.get("ingestion_skew_margin_seconds", 5.0))
    wan = wan_allowance_seconds(source_region, target_region, home_region, baseline)
    if effective_at.tzinfo is None:
        effective_at = effective_at.replace(tzinfo=timezone.utc)
    return effective_at + timedelta(seconds=float(window_seconds) - wan - skew)


def remaining_budget_seconds(absolute_deadline: Optional[datetime],
                             now: Optional[datetime] = None) -> Optional[float]:
    if absolute_deadline is None:
        return None
    now = now or datetime.now(timezone.utc)
    if absolute_deadline.tzinfo is None:
        absolute_deadline = absolute_deadline.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (absolute_deadline - now).total_seconds()
