# orchestrator/worker_controller.py — CPR side of worker-controller trust (D3).
"""Epoch-gated fence channel speaking to `worker/controller.py`.

Each fence command carries `operation_id` + `expected_execution_epoch`;
the controller enforces W2/W3/W4 and answers accept/refuse. Refusals are
NEVER silent: they flow through `refusal_sink` (T4 → reconciliation event
lane) and fail the fencing step closed — a refused fence is not a
confirmed fence (Protocols #4).

Live cutover is scheduled, not wired: `main.py` keeps the raw SSH
kill/verify hooks until workers ship a controller endpoint + instance-id
→ SSH-host resolution (Tracks A5/C2/D3). The emulated path binds a direct
in-process channel (see tests).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from orchestrator.models_v2 import new_ulid_like

logger = logging.getLogger(__name__)

# Refusal reason → reconciliation report event (inverse of the
# worker_telemetry REFUSAL_FINDING_MAP direction: reports file findings).
REFUSAL_REPORT_EVENTS = {
    "EPOCH_MISMATCH": "EPOCH_CONFLICT",
    "UNKNOWN_OPERATION": "OWNERSHIP_CONFLICT",
    "PAIRING_FAILED": "OWNERSHIP_CONFLICT",
}


def refusal_to_report(
    refusal: dict[str, Any], job_id: str = "", migration_id: str | None = None
) -> dict[str, Any]:
    """Map a T4 refusal doc to a `ReconciliationManager.report_event` dict."""
    reason = str((refusal or {}).get("reason", ""))
    return {
        "event_type": REFUSAL_REPORT_EVENTS.get(reason, "OWNERSHIP_CONFLICT"),
        "job_id": job_id or str((refusal or {}).get("job_id", "")),
        "migration_id": migration_id,
        "execution_epoch": int((refusal or {}).get("expected_execution_epoch", 0) or 0),
        "refusal": dict(refusal or {}),
    }


def file_refusal(
    refusal: dict[str, Any],
    reconciliation: Any,
    job_id: str = "",
    migration_id: str | None = None,
) -> list:
    """File a controller refusal as a reconciliation finding (W5 loop)."""
    if reconciliation is None:
        return []
    report = refusal_to_report(refusal, job_id, migration_id)
    if not report["job_id"]:
        return []
    return reconciliation.report_event(report)


def make_controller_fence_hooks(
    command_channel_factory: Callable[
        [str], Callable[[dict[str, Any]], dict[str, Any]]
    ],
    identity_resolver: Callable[[Any], dict[str, Any]],
    epoch_resolver: Callable[[Any], int] | None = None,
    refusal_sink: Callable[[dict[str, Any]], None] | None = None,
    command_metadata=None,
) -> dict[str, Callable[[Any], Any]]:
    """Fence hooks speaking the epoch-gated controller protocol.

    - `identity_resolver(plan)` → `{"host", "pid", "instance_id"}`.
    - `epoch_resolver(plan)` → expected execution epoch (default:
      `plan.execution_epoch` — the pre-rotation epoch, which a fresh
      controller accepts and a stale CPR fails against).
    - `refusal_sink(refusal_doc)` receives every T4 refusal for filing.
    """

    def _resolve(plan):
        ident = identity_resolver(plan) or {}
        host, pid, instance_id = (
            ident.get("host"),
            ident.get("pid"),
            ident.get("instance_id", ""),
        )
        if not host or pid is None:
            raise RuntimeError("controller fence: no target host/pid on plan")
        if epoch_resolver is not None:
            expected = int(epoch_resolver(plan))
        else:
            expected = int(getattr(plan, "execution_epoch", 0) or 0)
        return host, pid, instance_id, expected

    def _send(host: str, command: dict[str, Any]) -> dict[str, Any]:
        try:
            channel = command_channel_factory(host)
            return channel(command)
        except Exception as exc:
            raise RuntimeError(f"controller channel failed: {exc}")

    def _handle_refusal(result: dict[str, Any], action: str, plan=None):
        refusal = result.get("refusal") or {
            "reason": result.get("reason", "UNKNOWN"),
            "operation_id": result.get("operation_id", ""),
        }
        if plan is not None:
            refusal = {
                **refusal,
                "job_id": getattr(plan, "job_id", ""),
                "migration_id": getattr(plan, "migration_id", None),
            }
        if refusal_sink is not None:
            try:
                refusal_sink(dict(refusal))
            except Exception as exc:
                logger.warning("refusal sink failed: %s", exc)
        raise RuntimeError(
            f"controller refused {action}: {refusal.get('reason')} "
            f"(op {refusal.get('operation_id')})"
        )

    def _terminate(plan):
        host, pid, instance_id, expected = _resolve(plan)
        result = _send(
            host,
            {
                "instance_id": instance_id,
                **(command_metadata(plan) if command_metadata else {}),
                "operation_id": f"{plan.plan_id}:terminate"
                if getattr(plan, "plan_id", None)
                else new_ulid_like(),
                "expected_execution_epoch": expected,
                "action": "terminate",
                "pid": int(pid),
            },
        )
        if not result.get("accepted"):
            _handle_refusal(result, "terminate", plan)
        # Signal-sent is not fence-confirmed: _verify decides confirmation.

    def _verify(plan) -> bool:
        host, pid, instance_id, expected = _resolve(plan)
        result = _send(
            host,
            {
                "instance_id": instance_id,
                **(command_metadata(plan) if command_metadata else {}),
                "operation_id": f"{plan.plan_id}:verify"
                if getattr(plan, "plan_id", None)
                else new_ulid_like(),
                "expected_execution_epoch": expected,
                "action": "verify",
                "pid": int(pid),
            },
        )
        if not result.get("accepted"):
            _handle_refusal(result, "verify", plan)
        return bool(result.get("process_dead", False))

    def _invalidate(plan):
        return True

    return {"invalidate": _invalidate, "terminate": _terminate, "verify": _verify}
