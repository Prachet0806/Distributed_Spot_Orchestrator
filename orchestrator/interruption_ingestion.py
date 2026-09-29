# orchestrator/interruption_ingestion.py — Market/Risk Monitor ingestion lane.
"""Spot-interruption ingestion: worker flag file → SpotInterruptionEvent.

Ownership (component-architecture §2.2, Protocols §24.1): this lane owns
interruption-event ingestion + dedup and the authoritative
absolute-deadline computation at ingestion. The deadline is computed
ONCE here from the flag's `detected_at` (as `effective_at`) — never from
a fresh timestamp mid-migration — and carried immutably on the event
payload for the Planner/Coordinator.

Dedup: `stable_event_id()` derives a deterministic event_id from
(job_id, instance_id, minute-bucket of detected_at), so redelivered or
re-polled notices for the same interruption converge. Duplicate
`event_id`s are further refused by the EventLedger put-if-absent gate at
dispatch time (commit-before-dispatch, §7.2 rule 3).

Stale-checks (Protocols §8.5): consumers MUST evaluate epoch +
effective time before acting — see `is_stale_event()`.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from orchestrator.deadlines import (
    compute_absolute_deadline,
    remaining_budget_seconds,
    wan_allowance_seconds,
)
from orchestrator.protocol import EventType, SpotInterruptionEvent

logger = logging.getLogger(__name__)

PRODUCER = "market-risk-monitor"

# Fallback when the frozen baseline is unavailable (mirrors main.py).
DEFAULT_EMERGENCY_WINDOW_SECONDS = 120.0

# Dedup bucket: notices for the same (job, instance) inside one bucket
# are the same interruption (IMDS polls every 5 s; one notice per reclaim).
DEDUP_BUCKET_SECONDS = 60

# Recon-facing mapping for Event → report_event() dicts. SPOT_INTERRUPTION
# is deliberately absent: it belongs to the emergency lane, never to the
# reconciliation event lane.
_RECON_EVENT_TYPES = {
    "SOURCE_TERMINATED": "SOURCE_TERMINATED_UNCONFIRMED",
    "INFRASTRUCTURE_MISMATCH": "OWNERSHIP_CONFLICT",
    "VALIDATION_COMPLETED": "FENCE_UNCONFIRMED",
    "CHECKPOINT_PERSISTED": "FENCE_UNCONFIRMED",
    "PROVISIONING_COMPLETED": "TARGET_LOST",
}


def parse_flag_doc(obj: Any) -> Optional[Dict[str, Any]]:
    """Validate a spot-flag payload (`worker/spot_interrupt.py` shape).

    Returns `{detected_at, source, instance_id?}` with a tz-aware
    `detected_at`, or None when the doc is missing/malformed (never raises:
    transport loss and garbage are evidence-absence, not errors).
    """
    if not isinstance(obj, dict):
        return None
    raw = obj.get("detected_at")
    try:
        if isinstance(raw, datetime):
            detected_at = raw
        elif isinstance(raw, str) and raw:
            detected_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        else:
            return None
    except ValueError:
        return None
    if detected_at.tzinfo is None:
        detected_at = detected_at.replace(tzinfo=timezone.utc)
    return {
        "detected_at": detected_at,
        "source": str(obj.get("source", "imds")),
        "instance_id": obj.get("instance_id", ""),
    }


def stable_event_id(
    job_id: str,
    instance_id: str,
    detected_at: datetime,
    bucket_seconds: int = DEDUP_BUCKET_SECONDS,
) -> str:
    """Deterministic event_id: same interruption ⇒ same id (S21 dedup)."""
    if detected_at.tzinfo is None:
        detected_at = detected_at.replace(tzinfo=timezone.utc)
    bucket = int(detected_at.timestamp()) // int(bucket_seconds)
    return f"spot-interruption:{job_id}:{instance_id or 'unknown'}:{bucket}"


def _window_seconds(baseline: Optional[dict], override: Optional[float]) -> float:
    if override is not None:
        return float(override)
    try:
        return float((baseline or {}).get("execution", {}).get(
            "emergency_window_seconds", DEFAULT_EMERGENCY_WINDOW_SECONDS))
    except (TypeError, ValueError):
        return DEFAULT_EMERGENCY_WINDOW_SECONDS


def ingest_interruption(
    flag_doc: Any,
    *,
    job_id: str,
    execution_epoch: int = 0,
    instance_id: str = "",
    source_region: Optional[str] = None,
    target_region: Optional[str] = None,
    home_region: Optional[str] = None,
    baseline: Optional[dict] = None,
    window_seconds: Optional[float] = None,
    producer: str = PRODUCER,
    correlation_id: Optional[str] = None,
) -> Optional[SpotInterruptionEvent]:
    """Build the authoritative SpotInterruptionEvent for a flag notice.

    Returns None when the flag doc is missing/malformed (evidence-absence:
    the caller stays in its current regime). The absolute deadline is
    computed once here (§24.1) and stored on the payload; downstream code
    MUST reuse `payload["absolute_deadline"]`, never recompute.
    """
    parsed = parse_flag_doc(flag_doc)
    if parsed is None or not job_id:
        return None
    effective_at = parsed["detected_at"]
    instance = parsed["instance_id"] or instance_id
    window = _window_seconds(baseline, window_seconds)
    wan = wan_allowance_seconds(source_region, target_region, home_region, baseline)
    try:
        skew = float((baseline or {}).get("execution", {}).get(
            "ingestion_skew_margin_seconds", 5.0))
    except (TypeError, ValueError):
        skew = 5.0
    absolute_deadline = compute_absolute_deadline(
        effective_at, window, source_region, target_region, home_region, baseline)
    event_id = stable_event_id(job_id, instance, effective_at)
    noticed_at = datetime.now(timezone.utc)
    # Fidelity: how long the notice sat before the control plane saw it.
    # Large lag eats the provider window; surfaced for budget math + ops.
    try:
        detection_lag = max(0.0, (noticed_at - effective_at).total_seconds())
    except (TypeError, ValueError):
        detection_lag = 0.0
    return SpotInterruptionEvent(
        event_id=event_id,
        event_type=EventType.SPOT_INTERRUPTION,
        job_id=job_id,
        execution_epoch=int(execution_epoch or 0),
        migration_id=None,
        correlation_id=correlation_id or event_id,
        occurred_at=noticed_at,
        effective_at=effective_at,
        producer=producer,
        payload={
            "instance_id": instance,
            "flag_source": parsed["source"],
            "source_region": source_region,
            "target_region": target_region,
            "home_region": home_region,
            "window_seconds": window,
            "wan_allowance_seconds": wan,
            "ingestion_skew_margin_seconds": skew,
            "absolute_deadline": absolute_deadline.isoformat(),
            "noticed_at": noticed_at.isoformat(),
            "detection_lag_seconds": detection_lag,
        },
    )


def is_stale_event(
    event: SpotInterruptionEvent,
    job: Optional[Dict[str, Any]],
    now: Optional[datetime] = None,
) -> Tuple[bool, str]:
    """Protocols §8.5 stale-check: epoch + deadline before acting.

    Returns (True, reason) when the event MUST be dropped, (False, "fresh")
    when it is actionable. Missing job ⇒ stale (no authority to act on).
    """
    if job is None:
        return True, "no-job-record"
    current_epoch = int(job.get("execution_epoch", 0) or 0)
    if int(getattr(event, "execution_epoch", 0) or 0) != current_epoch:
        return True, (
            f"epoch-mismatch: event={event.execution_epoch} "
            f"registry={current_epoch}")
    raw_deadline = (event.payload or {}).get("absolute_deadline")
    if raw_deadline:
        try:
            deadline = datetime.fromisoformat(str(raw_deadline).replace("Z", "+00:00"))
        except ValueError:
            return True, "unparseable-deadline"
        remaining = remaining_budget_seconds(deadline, now)
        if remaining is not None and remaining <= 0:
            return True, f"deadline-exceeded: {remaining:.0f}s remaining"
    return False, "fresh"


def read_spot_flag(host: str, flag_path: str, log=None) -> Optional[Dict[str, Any]]:
    """Fetch + parse the worker interruption flag over SSH.

    Returns the raw flag doc (dict) or None on transport/parse failure.
    Presence-without-content still counts as evidence: callers that only
    need presence should keep using a presence check; this reader adds
    `detected_at` fidelity for deadline math.
    """
    import json

    try:
        from orchestrator.utils import SSHClient
        ssh = SSHClient(host)
        ssh.connect()
        try:
            result = ssh.run_command(f"cat {flag_path}", check=False)
            stdout = (getattr(result, "stdout", "") or "").strip()
        finally:
            try:
                ssh.close()
            except Exception:
                pass
        if not stdout:
            return None
        try:
            return json.loads(stdout)
        except ValueError:
            return None
    except Exception as exc:
        if log is not None:
            try:
                log.warning("Spot flag read failed for %s: %s", host, exc)
            except Exception:
                pass
        return None


def dispatch_to_reconciliation(event: Any, reconciliation: Any) -> list:
    """Fan-out adapter: protocol Event → ReconciliationManager.report_event.

    SPOT_INTERRUPTION belongs to the emergency lane and returns [] here
    (never auto-filed as a finding). Unknown types return [] — silently
    filing findings for unmapped types would fabricate ownership doubt.
    """
    if reconciliation is None:
        return []
    etype = getattr(event, "event_type", "")
    etype = getattr(etype, "value", etype)
    etype = str(etype or "")
    mapped = _RECON_EVENT_TYPES.get(etype)
    if mapped is None:
        return []
    payload = dict(getattr(event, "payload", None) or {})
    report = {
        "event_type": mapped,
        "job_id": getattr(event, "job_id", ""),
        "migration_id": getattr(event, "migration_id", None),
        "execution_epoch": int(getattr(event, "execution_epoch", 0) or 0),
        **payload,
    }
    if not report["job_id"]:
        return []
    return reconciliation.report_event(report)


def build_dispatcher(event_ledger: Any, reconciliation: Any = None):
    """Construct the CPR event dispatcher with standard subscriptions.

    Ledger commit-before-dispatch is enforced inside EventDispatcher
    (dedup gate per (event_id, consumer_id)). The reconciliation fan-out
    resolves `reconciliation` lazily so callers can subscribe before the
    manager is constructed.
    """
    from orchestrator.event_dispatcher import EventConsumer, EventDispatcher

    dispatcher = EventDispatcher(event_ledger=event_ledger)
    # Late-bind cell: main.py constructs reconciliation after the
    # dispatcher; set_reconciliation_manager() fills it before the loop.
    cell: Dict[str, Any] = {"manager": reconciliation}

    def _recon_fanout(event):
        manager = cell["manager"]
        if manager is None:
            return
        dispatch_to_reconciliation(event, manager)

    def set_manager(manager):
        cell["manager"] = manager

    for etype in sorted(_RECON_EVENT_TYPES):
        dispatcher.register_consumer(
            etype, EventConsumer(f"reconciliation-{etype.lower()}", _recon_fanout))
    dispatcher.set_reconciliation_manager = set_manager  # type: ignore[attr-defined]
    return dispatcher
