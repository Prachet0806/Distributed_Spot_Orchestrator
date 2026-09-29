# orchestrator/validator_probes.py — production L1/L2 probes (Track C2).
"""Transport-backed validator probes replacing the `stub-v1` pass-throughs.

Both probes are pure over injected collaborators (no AWS calls) and work
with any `WorkerTransport` (`EmulatedWorkerTransport` locally,
`SSHWorkerTransport` on AWS):

- L1 infrastructure: `ps -p <restored-pid>` on the target host. Reachable
  + running ⇒ pass. Process gone ⇒ fail (evidence of unhealth). Transport
  error ⇒ fail-closed (cannot confirm health ⇒ not healthy).
- L2 application: T1 heartbeat freshness via `WorkerTelemetryReader`.
  READY ⇒ pass. STALE/MISSING ⇒ fail with `inconclusive: True` — evidence
  absence routes to INCONCLUSIVE → Recovery Policy (telemetry-contract
  gating rule, S19), never to silent promotion.

Execution context (`target_host`, `target_pid`) arrives per-migration via
the Coordinator's `execution_context_sink` (Sprint 2); without context —
or without an SSH-reachable host — probes report inconclusive, never
stub-pass (I7/I15). NOTE (live backlog): the sink currently publishes the
provisioner `instance_id` as `target_host`; instance-id → IP resolution
(EC2 describe) lands with the live provisioner path (Track A5/C2).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from orchestrator.worker_telemetry import WorkerTelemetryReader, heartbeat_liveness

logger = logging.getLogger(__name__)

PROBE_VERSION = "transport-v1"


def make_context_provider(store: Dict[str, Dict[str, Any]]):
    """Build a `(migration_id) -> ctx | None` reader over a mutable map.

    The map is filled by the Coordinator's `execution_context_sink` at
    restore time; keyed by `migration_id` (unique per attempt).
    """
    def _provider(migration_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if not migration_id:
            return None
        ctx = store.get(migration_id)
        return dict(ctx) if ctx is not None else None
    return _provider


def make_infra_probe(
    transport_factory: Callable[[str], Any],
    context_provider: Optional[Callable[[Optional[str]], Optional[Dict[str, Any]]]] = None,
    timeout: float = 30.0,
):
    """L1 probe: restored process alive on the target host.

    The returned callable matches the `Validator(infra_probe=...)` seam:
    `(migration_id=None, **kw) -> details`, where `details["passed"]`
    decides the check and `details["inconclusive"]` marks evidence-absence.
    """
    def _probe(migration_id: Optional[str] = None, **_kw) -> Dict[str, Any]:
        ctx = context_provider(migration_id) if callable(context_provider) else None
        if not ctx:
            return {"passed": False, "inconclusive": True,
                    "error": "no-execution-context", "probe": PROBE_VERSION}
        host, pid = ctx.get("target_host"), ctx.get("target_pid")
        if not host:
            return {"passed": False, "inconclusive": True,
                    "error": "no-target-host", "probe": PROBE_VERSION}
        if pid is None:
            return {"passed": False, "inconclusive": True,
                    "error": "no-target-pid", "probe": PROBE_VERSION}
        try:
            res = transport_factory(host).run_command(
                f"ps -p {int(pid)}", timeout=timeout)
        except Exception as exc:
            # Fail-closed: an unreachable target is not a healthy target.
            return {"passed": False, "target_reachable": False,
                    "process_running": False, "error": str(exc),
                    "probe": PROBE_VERSION}
        running = int((res or {}).get("returncode", 1)) == 0
        return {"passed": running, "target_reachable": True,
                "process_running": running, "pid": int(pid),
                "probe": PROBE_VERSION}
    return _probe


def make_app_probe(
    transport_factory: Callable[[str], Any],
    context_provider: Optional[Callable[[Optional[str]], Optional[Dict[str, Any]]]] = None,
    workspace_root: str = "/opt/job_workspace",
    clock: Optional[Callable[[], datetime]] = None,
    timeout: float = 15.0,
):
    """L2 probe: workload responding per T1 heartbeat freshness.

    STALE/MISSING heartbeats ⇒ `inconclusive: True` (degraded evidence,
    not proof of failure); transport loss ⇒ same (loss degrades
    confidence, never fabricates health — telemetry-contract §T1).
    """
    _clock = clock or (lambda: datetime.now(timezone.utc))

    def _probe(migration_id: Optional[str] = None, **_kw) -> Dict[str, Any]:
        ctx = context_provider(migration_id) if callable(context_provider) else None
        if not ctx or not ctx.get("target_host"):
            return {"passed": False, "inconclusive": True,
                    "error": "no-execution-context", "probe": PROBE_VERSION}
        try:
            reader = WorkerTelemetryReader(
                transport_factory(ctx["target_host"]),
                workspace_root=workspace_root, clock=_clock)
            verdict = reader.liveness()
        except Exception as exc:
            return {"passed": False, "inconclusive": True,
                    "error": f"telemetry-read-failed: {exc}",
                    "probe": PROBE_VERSION}
        if verdict.get("readiness") == "READY":
            return {"passed": True, "workload_responding": True,
                    "liveness": verdict.get("liveness"),
                    "age_seconds": verdict.get("age_seconds"),
                    "probe": PROBE_VERSION}
        return {"passed": False, "inconclusive": True,
                "workload_responding": False,
                "liveness": verdict.get("liveness"),
                "age_seconds": verdict.get("age_seconds"),
                "probe": PROBE_VERSION}
    return _probe
