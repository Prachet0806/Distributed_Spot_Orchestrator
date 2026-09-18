# orchestrator/step_retry.py — §10.8 step retry, backoff, and priority semantics.
"""Execution directives for PlanStep retry behavior (Protocols §10.8).

Rules implemented (normative):
1. Attempt budget: at most `max_attempts` (ADR-012 default 3); every attempt
   reuses the step's `operation_id` (I11); a new ID means a new operation.
2. Retryable codes only: FAILED retries iff its code is listed AND attempt +
   migration budgets AND the absolute deadline remain valid (§6.1).
3. Backoff: exponential base→max ±25% jitter; sleep consumes effective_deadline.
4. UNKNOWN is not an attempt: enters outcome resolution within the UNKNOWN
   budget; only a resolved retryable FAILED retries; unresolved ⇒ reconciling.
5. Priority: lower value schedules first under contention; criticality
   preempts — BEST_EFFORT never delays SAFETY_CRITICAL.
6. `failure_injection_points`: test-only; production plans carry an empty
   list; the Coordinator rejects non-empty lists unless test harness is set.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

# ADR-012 defaults (also pinned in config/v2_baseline.yaml → retry).
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_BASE_SECONDS = 2.0
DEFAULT_BACKOFF_MAX_SECONDS = 30.0
DEFAULT_BACKOFF_JITTER = 0.25
DEFAULT_UNKNOWN_RESOLUTION_BUDGET_SECONDS = 30.0

# Conservative default retryable sets: timeout/transient execution failures.
# Verdict-like steps (VALIDATE), ownership steps (FENCE/ACTIVATE), and
# best-effort steps (PRECHECK/FINALIZE/CLEANUP) never step-retry: failures
# escalate to Recovery Policy / reconciliation instead.
DEFAULT_RETRYABLE: dict[str, frozenset] = {
    "CHECKPOINT": frozenset({"CRIU_DUMP_TIMEOUT", "CRIU_DUMP_FAILED"}),
    "PERSIST": frozenset({"CHECKPOINT_PERSIST_FAILED", "CHECKPOINT_TRANSFER_TIMEOUT"}),
    "PROVISION": frozenset({"PROVISION_FAILED", "CAPACITY_UNAVAILABLE", "OPERATION_TIMEOUT"}),
    "TRANSFER": frozenset({"TRANSFER_FAILED", "TRANSFER_TIMEOUT"}),
    "RESTORE": frozenset({"CRIU_RESTORE_TIMEOUT", "CRIU_RESTORE_FAILED"}),
    "VALIDATE": frozenset(),
    "FENCE": frozenset(),
    "ACTIVATE": frozenset(),
    "FINALIZE": frozenset(),
    "PRECHECK": frozenset(),
    "CLEANUP": frozenset(),
}

# Lower value = scheduled earlier under contention (§10.8 rule 5).
DEFAULT_PRIORITY: dict[str, int] = {
    "FENCE": 10,
    "CHECKPOINT": 20,
    "RESTORE": 20,
    "VALIDATE": 20,
    "PERSIST": 30,
    "PROVISION": 30,
    "TRANSFER": 30,
    "ACTIVATE": 30,
    "PRECHECK": 40,
    "FINALIZE": 100,
    "CLEANUP": 100,
}

# Criticality preempts priority: anything BEST_EFFORT sorts after all
# correctness/safety/execution work under contention.
_CRITICALITY_CLASS = {
    "SAFETY_CRITICAL": 0,
    "CORRECTNESS_CRITICAL": 0,
    "EXECUTION_CRITICAL": 0,
    "BEST_EFFORT": 1,
}


class CodedError(Exception):
    """Executor failure carrying a taxonomy code (Protocols §19).

    Executors raise this (instead of bare exceptions) so the Coordinator
    can apply retryable-code gating. `failure_code` unlisted ⇒ escalate.
    """

    def __init__(self, failure_code: str, message: str = "",
                 transience: str = "UNKNOWN"):
        super().__init__(message or failure_code)
        self.failure_code = failure_code
        self.transience = transience


def failure_code_of(exc: BaseException, default: str) -> str:
    """Extract a taxonomy code from an executor error.

    Uncoded errors return `default` — and defaults are deliberately NOT in
    any retryable set, so unknown failures escalate instead of retrying.
    """
    code = getattr(exc, "failure_code", None) or getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code
    return default


@dataclass(frozen=True)
class StepRetrySpec:
    """Resolved §10.8 directives for one step execution."""

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS
    backoff_max_seconds: float = DEFAULT_BACKOFF_MAX_SECONDS
    backoff_jitter: float = DEFAULT_BACKOFF_JITTER
    retryable_failure_codes: frozenset = field(default_factory=frozenset)
    unknown_resolution_budget_seconds: float = DEFAULT_UNKNOWN_RESOLUTION_BUDGET_SECONDS
    priority: int = 100
    criticality: str = "EXECUTION_CRITICAL"
    failure_injection_points: tuple = field(default_factory=tuple)


def spec_for(
    step_type: str,
    *,
    max_attempts: Optional[int] = None,
    backoff_base_seconds: Optional[float] = None,
    backoff_max_seconds: Optional[float] = None,
    retryable_failure_codes: Optional[Iterable[str]] = None,
    unknown_resolution_budget_seconds: Optional[float] = None,
    priority: Optional[int] = None,
    criticality: Optional[str] = None,
    failure_injection_points: Optional[Iterable[str]] = None,
    baseline: Optional[dict] = None,
) -> StepRetrySpec:
    """Build a spec: explicit args > plan-step values > ADR-012/baseline defaults."""
    base = (baseline or {}).get("retry", {}) if baseline else {}
    key = str(step_type).upper()
    return StepRetrySpec(
        max_attempts=int(max_attempts if max_attempts is not None
                         else base.get("operation_max_attempts", DEFAULT_MAX_ATTEMPTS)),
        backoff_base_seconds=float(backoff_base_seconds if backoff_base_seconds is not None
                                   else base.get("operation_backoff_base_seconds",
                                                 DEFAULT_BACKOFF_BASE_SECONDS)),
        backoff_max_seconds=float(backoff_max_seconds if backoff_max_seconds is not None
                                  else base.get("operation_backoff_max_seconds",
                                                DEFAULT_BACKOFF_MAX_SECONDS)),
        backoff_jitter=float(base.get("backoff_jitter", DEFAULT_BACKOFF_JITTER)),
        retryable_failure_codes=frozenset(retryable_failure_codes
                                          if retryable_failure_codes is not None
                                          else DEFAULT_RETRYABLE.get(key, frozenset())),
        unknown_resolution_budget_seconds=float(
            unknown_resolution_budget_seconds if unknown_resolution_budget_seconds is not None
            else base.get("unknown_resolution_timeout_seconds",
                          DEFAULT_UNKNOWN_RESOLUTION_BUDGET_SECONDS)),
        priority=int(priority if priority is not None
                     else DEFAULT_PRIORITY.get(key, 100)),
        criticality=str(criticality or ("SAFETY_CRITICAL" if key == "FENCE"
                                        else "BEST_EFFORT" if key in ("PRECHECK", "FINALIZE", "CLEANUP")
                                        else "EXECUTION_CRITICAL")),
        failure_injection_points=tuple(failure_injection_points or ()),
    )


def should_retry(
    spec: StepRetrySpec,
    *,
    failure_code: Optional[str],
    attempts_used: int,
    budgets_valid: bool,
) -> bool:
    """Rule 1+2: retry iff code listed AND attempts remain AND budgets valid.

    `attempts_used` counts FAILED attempts (UNKNOWN outcomes never increment
    it — rule 4). Unlisted/unknown codes return False (escalate).
    """
    if not budgets_valid:
        return False
    if attempts_used >= spec.max_attempts:
        return False
    return bool(failure_code) and failure_code in spec.retryable_failure_codes


def backoff_delay_seconds(
    spec: StepRetrySpec,
    attempts_used: int,
    rng: Optional[Callable[[float, float], float]] = None,
) -> float:
    """Rule 3: exponential base→max with ±jitter (default 25%).

    `attempts_used` is 1-based (delay before retry N+1 grows with N).
    Pass an `rng` in tests for determinism.
    """
    exp = spec.backoff_base_seconds * (2.0 ** max(0, attempts_used - 1))
    capped = min(exp, spec.backoff_max_seconds)
    jitter = spec.backoff_jitter
    lo, hi = capped * (1.0 - jitter), capped * (1.0 + jitter)
    if rng is not None:
        return rng(lo, hi)
    return random.uniform(lo, hi)


def reject_injection_points(steps: Iterable[Any], *, test_harness: bool) -> None:
    """Rule 6: production plans MUST carry empty failure_injection_points."""
    offenders = []
    for step in steps or []:
        points = (step.get("failure_injection_points")
                  if isinstance(step, dict)
                  else getattr(step, "failure_injection_points", None))
        if points:
            name = (step.get("step_id", "?") if isinstance(step, dict)
                    else getattr(step, "step_id", "?"))
            offenders.append(str(name))
    if offenders and not test_harness:
        raise ValueError(
            "plan carries failure_injection_points on steps "
            f"{offenders}; rejected outside test harness (§10.8 rule 6)")


def _criticality_class(crit: Any) -> int:
    """Map a criticality (enum, name, or qualified string) to its class."""
    name = getattr(crit, "name", crit)  # enums → member name
    text = str(name if name is not None else "EXECUTION_CRITICAL").upper()
    if "." in text:  # tolerate str(EnumType.MEMBER) form
        text = text.rsplit(".", 1)[1]
    return _CRITICALITY_CLASS.get(text, 0)


def schedule_order(items: Iterable[Any]) -> list:
    """Rule 5: contention order — criticality class, then priority.

    BEST_EFFORT work (CLEANUP/FINALIZE/PRECHECK) never schedules ahead of
    safety/correctness/execution work regardless of numeric priority.
    Items expose `.priority` / `.criticality` attrs (or dict keys); ties
    keep input order (stable sort).
    """

    def _key(item: Any):
        if isinstance(item, dict):
            pri = item.get("priority", 100)
            crit = item.get("criticality", "EXECUTION_CRITICAL")
        else:
            pri = getattr(item, "priority", 100)
            crit = getattr(item, "criticality", "EXECUTION_CRITICAL")
        return (_criticality_class(crit), pri)

    return sorted(items, key=_key)
