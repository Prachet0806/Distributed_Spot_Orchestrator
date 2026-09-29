# worker/controller.py — emulated worker controller (Track D3, W1–W5).
"""The only worker-side component permitted to act on control commands
(component-architecture §12.6). Emulated here over injected collaborators;
the live controller binds identity from the EC2 instance identity document
(IMDS) and speaks over the SSH control channel (cutover scheduled).

Trust ceremonies implemented:

- W1 lifecycle: boot binds identity → heartbeat/preflight evidence →
  fenceable at all times → no autonomous decisions, ever (I8).
- W2 IMDS identity binding: every command is checked against
  `(instance_id, admission_epoch)` recorded at boot; anything outside the
  binding is refused with PAIRING_FAILED. Locally the binding comes from
  the environment (`INSTANCE_ID`, `WORKLOAD_EXECUTION_EPOCH`); live it
  comes from the IMDS identity document.
- W3 epoch-gated commands: `expected_execution_epoch >= admission_epoch`
  or the command is refused with EPOCH_MISMATCH — a stale or partitioned
  CPR cannot fence a worker that legitimately moved forward.
- W4 operation_id dedup: controller-side replay table; repeated identical
  commands converge without re-executing (I7).
- W5 refusal telemetry: every refusal emits a `refusal/v1` doc into the
  outbox (findings-grade evidence, never silent drops); the CPR client
  files these into the reconciliation event lane.

Autonomy prohibition (I8): the controller executes PRIMITIVES ONLY
(terminate / verify / preflight). Decision-shaped commands (migrate,
recover, replan, supersede, fence-as-decision, …) are refused with
UNKNOWN_OPERATION. A local interruption flag is evidence for the control
plane, never a trigger — this module has no migration entrypoint.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

PRIMITIVE_ACTIONS = ("terminate", "verify", "preflight")


class ControllerError(RuntimeError):
    """Refused or failed control command (fail-closed, always explicit)."""


def boot_identity(instance_id: Optional[str] = None,
                  admission_epoch: Optional[int] = None) -> Dict[str, Any]:
    """Resolve the W2 binding: explicit args win, environment is fallback.

    Live: `instance_id` comes from the IMDS identity document and
    `admission_epoch` from pool-join metadata. Emulated: `INSTANCE_ID` /
    `WORKLOAD_EXECUTION_EPOCH` (same vars the heartbeat path uses).
    """
    iid = instance_id if instance_id is not None else os.getenv("INSTANCE_ID", "")
    if admission_epoch is None:
        try:
            admission_epoch = int(os.getenv("WORKLOAD_EXECUTION_EPOCH", "0"))
        except (TypeError, ValueError):
            admission_epoch = 0
    if not iid:
        raise ControllerError("refusing boot without instance identity (W2)")
    return {"instance_id": str(iid), "admission_epoch": int(admission_epoch)}


class WorkerController:
    """Emulated fence/evidence channel for one worker instance."""

    def __init__(
        self,
        instance_id: Optional[str] = None,
        admission_epoch: Optional[int] = None,
        executor: Any = None,
        clock: Optional[Callable[[], datetime]] = None,
    ):
        binding = boot_identity(instance_id, admission_epoch)
        self.instance_id: str = binding["instance_id"]
        self.admission_epoch: int = binding["admission_epoch"]
        self._executor = executor
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._replay: Dict[str, Dict[str, Any]] = {}
        self.refusals: List[Dict[str, Any]] = []
        self.command_log: List[Dict[str, Any]] = []

    # -- W1 lifecycle evidence --
    def heartbeat(self, job_id: str = "local-job",
                  pid: Optional[int] = None) -> Dict[str, Any]:
        """T1 heartbeat/v1 report (validates via worker_telemetry.parse_heartbeat)."""
        if pid is None:
            pid = os.getpid()
        return {
            "schema": "heartbeat/v1",
            "job_id": job_id,
            "instance_id": self.instance_id,
            "execution_epoch": self.admission_epoch,
            "pid": int(pid),
            "observed_at": self._clock().isoformat(),
            "workload": {"state": "RUNNING"},
        }

    def preflight(self) -> Any:
        """Delegate to the executor preflight hook (criu_wrapper.sh preflight live)."""
        hook = getattr(self._executor, "preflight", None)
        if not callable(hook):
            raise ControllerError("no preflight executor bound")
        return hook()

    # -- command channel (W2/W3/W4/I8) --
    def handle_command(self, command: Dict[str, Any]) -> Dict[str, Any]:
        """Authorize + execute one control command. Never raises for
        authorization outcomes: refusals are returned as
        `{"accepted": False, "refusal": {...}}` AND filed in the outbox."""
        if not isinstance(command, dict):
            return self._refuse("", None, "UNKNOWN_OPERATION")
        operation_id = str(command.get("operation_id", "") or "")
        if operation_id and operation_id in self._replay:
            return dict(self._replay[operation_id])  # W4: converge, no re-exec
        if command.get("instance_id") != self.instance_id:
            return self._refuse(operation_id,
                                command.get("expected_execution_epoch"),
                                "PAIRING_FAILED")
        expected = command.get("expected_execution_epoch")
        try:
            expected_epoch = int(expected)
        except (TypeError, ValueError):
            return self._refuse(operation_id, expected, "EPOCH_MISMATCH")
        if expected_epoch < self.admission_epoch:
            return self._refuse(operation_id, expected, "EPOCH_MISMATCH")
        action = command.get("action")
        if action not in PRIMITIVE_ACTIONS:
            # I8: decision-shaped commands are not executable primitives.
            return self._refuse(operation_id, expected, "UNKNOWN_OPERATION")
        result = self._execute(action, command, operation_id)
        if operation_id:
            self._replay[operation_id] = dict(result)
        return result

    # -- internals --
    def _execute(self, action: str, command: Dict[str, Any],
                 operation_id: str) -> Dict[str, Any]:
        self.command_log.append(dict(command))
        if self._executor is None:
            raise ControllerError("no primitive executor bound")
        if action == "terminate":
            pid = command.get("pid")
            if pid is None:
                raise ControllerError("terminate needs a pid")
            self._executor.terminate(int(pid))
            return {"accepted": True, "operation_id": operation_id,
                    "action": action, "pid": int(pid)}
        if action == "verify":
            pid = command.get("pid")
            if pid is None:
                raise ControllerError("verify needs a pid")
            dead = bool(self._executor.verify(int(pid)))
            return {"accepted": True, "operation_id": operation_id,
                    "action": action, "pid": int(pid),
                    "process_dead": dead}
        if action == "preflight":
            out = self._executor.preflight()
            return {"accepted": True, "operation_id": operation_id,
                    "action": action, "output": out}
        raise ControllerError(f"unreachable action {action!r}")  # pragma: no cover

    def _refuse(self, operation_id: str, expected: Any,
                reason: str) -> Dict[str, Any]:
        refusal = {
            "schema": "refusal/v1",
            "instance_id": self.instance_id,
            "admission_epoch": self.admission_epoch,
            "operation_id": operation_id,
            "expected_execution_epoch": expected,
            "reason": reason,
            "observed_at": self._clock().isoformat(),
        }
        self.refusals.append(refusal)
        return {"accepted": False, "operation_id": operation_id,
                "reason": reason, "refusal": dict(refusal)}
