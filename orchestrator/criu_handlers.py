# orchestrator/criu_handlers.py — CRIU dump/restore over a WorkerTransport.
"""Builds CheckpointManager handlers from the `criu_wrapper.sh` exit-code
contract (Protocols #9 §11.8). Exit codes are the API: 0 ok, 1 criu error,
2 timeout, 3 preflight/PARTIAL, 4 incompatible environment.
"""
import shlex
import json
import time
import uuid
import tempfile
import logging
from pathlib import Path

from orchestrator.checkpoint_manager import CheckpointResult, RestoreResult

WRAPPER_PATH = "/opt/spot-worker-control/checkpoint/criu_wrapper.sh"
logger = logging.getLogger(__name__)


def make_dump_handler(transport, wrapper_path: str = WRAPPER_PATH,
                      workspace_root: str = "/opt/job_workspace"):
    def _dump(*, job_id: str, pid: int, host=None, timeout: float = 280.0, operation_id=None):
        checkpoint_id = "chk-" + (operation_id or uuid.uuid4().hex)
        images_dir = f"{workspace_root}/checkpoints/{checkpoint_id}"
        cmd = (f"sudo -n bash {shlex.quote(wrapper_path)} dump "
               f"{int(pid)} {shlex.quote(images_dir)}")
        started = time.monotonic()
        try:
            res = transport.run_command(cmd, timeout=timeout)
        except Exception as exc:
            raise RuntimeError(f"dump transport failed for {job_id}: {exc}")
        rc = int(res.get("returncode", 1))
        if rc == 2:
            raise TimeoutError(f"dump timed out for {job_id}: {res.get('stderr', '')}")
        if rc == 3:
            raise RuntimeError(f"dump preflight failed for {job_id}: "
                               f"{res.get('stderr', '')}")
        if rc != 0:
            raise RuntimeError(f"CRIU_DUMP_FAILED for {job_id}: "
                               f"{res.get('stderr', '')}")
        local_root = Path(tempfile.mkdtemp(prefix="checkpoint-images-"))
        local = local_root / "images"
        transport.fetch_directory(images_dir, str(local))
        import hashlib
        digest = hashlib.sha256()
        size = 0
        for image in sorted(local.rglob("*")):
            if image.is_file():
                data = image.read_bytes()
                digest.update(str(image.relative_to(local)).encode())
                digest.update(data)
                size += len(data)
        return CheckpointResult(checkpoint_id=checkpoint_id, size_bytes=size,
            duration_seconds=time.monotonic() - started, digest=digest.hexdigest(),
            status="LOCAL", job_id=job_id, lineage_id=job_id, local_path=str(local),
            snapshot=_checkpoint_snapshot(local))
    return _dump


def make_restore_handler(transport, wrapper_path: str = WRAPPER_PATH,
                         workspace_root: str = "/opt/job_workspace"):
    def _restore(*, checkpoint_id: str, host=None, timeout: float = 170.0,
                 operation_id=None):
        if not operation_id:
            raise ValueError("restore requires its durable operation identity")
        images_dir = f"{workspace_root}/checkpoints/{checkpoint_id}"
        cmd = ("sudo -n python3 -I /opt/spot-worker-control/control_entry.py "
               f"restore run --operation-id {shlex.quote(operation_id)} "
               f"--images-dir {shlex.quote(images_dir)}")
        started = time.monotonic()
        try:
            res = transport.run_command(cmd, timeout=timeout)
        except Exception:
            return RestoreResult(checkpoint_id=checkpoint_id, success=False,
                                 duration_seconds=time.monotonic() - started,
                                 outcome="UNKNOWN")
        rc = int(res.get("returncode", 1))
        duration = time.monotonic() - started
        if rc != 0:
            logger.error("CRIU restore %s returned %s: %s %s", checkpoint_id, rc,
                         res.get("stdout", ""), res.get("stderr", ""))
        try:
            receipt = json.loads(res.get("stdout", ""))
        except (TypeError, ValueError):
            receipt = {}
        outcome = receipt.get("state", "UNKNOWN")
        if outcome == "UNKNOWN" or rc == 2:
            return RestoreResult(checkpoint_id=checkpoint_id, success=False,
                                 duration_seconds=duration, outcome="UNKNOWN")
        if outcome == "FAILED" or rc != 0:
            return RestoreResult(checkpoint_id=checkpoint_id, success=False,
                                 duration_seconds=duration, outcome="FAILED")
        if outcome != "SUCCEEDED":
            return RestoreResult(checkpoint_id=checkpoint_id, success=False,
                                 duration_seconds=duration, outcome="UNKNOWN")
        result = receipt.get("result") or {}
        return RestoreResult(checkpoint_id=checkpoint_id, success=True,
                             duration_seconds=duration, process_id=result.get("process_id"),
                             outcome="SUCCEEDED", process_start=result.get("process_start", ""),
                             contained=bool(result.get("contained")))
    return _restore


def make_verify_handler(transport, wrapper_path: str = WRAPPER_PATH,
                        workspace_root: str = "/opt/job_workspace"):
    def _verify(*, checkpoint_dir: str = None, timeout: float = 60.0) -> str:
        images_dir = checkpoint_dir or f"{workspace_root}/checkpoint"
        cmd = (f"sudo -n bash {shlex.quote(wrapper_path)} verify "
               f"{shlex.quote(images_dir)}")
        res = transport.run_command(cmd, timeout=timeout)
        rc = int(res.get("returncode", 2))
        return {0: "VALID", 1: "INVALID"}.get(rc, "INDETERMINATE")
    return _verify


def make_fence_hooks(transport, sudo_kill: bool = True):
    """Terminate + verify hooks for the coordinator fencing step."""
    def _terminate(plan):
        job = {"pid": getattr(plan, "pid", None)}
        pid = job.get("pid")
        if not pid:
            raise RuntimeError("fence terminate: no source pid on plan")
        kill = "sudo kill -9" if sudo_kill else "kill -9"
        # Signal-sent is not fence-confirmed: a nonzero rc (e.g. already
        # gone) must not fail fencing here — _verify decides confirmation.
        transport.run_command(f"{kill} {int(pid)}", timeout=30.0)

    def _verify(plan):
        pid = getattr(plan, "pid", None)
        res = transport.run_command(f"ps -p {int(pid)}", timeout=30.0)
        # ps exits nonzero when the pid is gone → terminated.
        rc = int(res.get("returncode", 0))
        if rc not in (0, 1):
            raise RuntimeError("fencing verification transport is uncertain")
        return rc == 1

    def _invalidate(plan):
        # Epoch rotation itself happens in the coordinator via the registry;
        # this hook exists so deployments can add pre-invalidation checks.
        return True

    return {"invalidate": _invalidate, "terminate": _terminate, "verify": _verify}


def _transport_for_host(transport_factory, host: str):
    if not host:
        raise RuntimeError("CRIU operation needs a target host: refusing to guess")
    return transport_factory(host)


def make_dump_handler_for(transport_factory, wrapper_path: str = WRAPPER_PATH,
                          workspace_root: str = "/opt/job_workspace"):
    """Dump handler resolving the worker transport per call host."""
    def _dump(*, job_id: str, pid: int, host=None, timeout: float = 280.0, operation_id=None):
        transport = _transport_for_host(transport_factory, host)
        return make_dump_handler(transport, wrapper_path, workspace_root)(
            job_id=job_id, pid=pid, host=host, timeout=timeout, operation_id=operation_id)
    return _dump


def make_restore_handler_for(transport_factory, wrapper_path: str = WRAPPER_PATH,
                             workspace_root: str = "/opt/job_workspace"):
    """Restore handler resolving the worker transport per call host."""
    def _restore(*, checkpoint_id: str, host=None, timeout: float = 170.0,
                 operation_id=None):
        transport = _transport_for_host(transport_factory, host)
        return make_restore_handler(transport, wrapper_path, workspace_root)(
            checkpoint_id=checkpoint_id, host=host, timeout=timeout,
            operation_id=operation_id)
    return _restore


def make_restore_status_provider_for(transport_factory, operation_store,
                                     plan_store, workspace_root="/opt/job_workspace"):
    """Resolve a lost restore response from the target's root-owned journal."""
    def _status(operation_id):
        try:
            operation = operation_store.get(operation_id)
            request = operation["request"]
            if request.get("step_name") != "restoring":
                return {"state": "UNKNOWN"}
            plan = plan_store.get_plan(request["plan_id"])
            context = plan.get("execution_context") or {}
            host, checkpoint_id = context.get("target_host"), context.get("checkpoint_id")
            if not host or not checkpoint_id:
                return {"state": "UNKNOWN"}
            images_dir = f"{workspace_root}/checkpoints/{checkpoint_id}"
            command = ("sudo -n python3 -I /opt/spot-worker-control/control_entry.py "
                       f"restore status --operation-id {shlex.quote(operation_id)} "
                       f"--images-dir {shlex.quote(images_dir)}")
            response = transport_factory(host).run_command(command, timeout=30)
            if response["returncode"] != 0:
                return {"state": "UNKNOWN"}
            return json.loads(response["stdout"])
        except Exception as exc:
            logger.warning("restore status probe uncertain for %s: %s", operation_id, exc)
            return {"state": "UNKNOWN"}
    return _status


def make_fence_hooks_for(transport_factory, sudo_kill: bool = True,
                         pid_resolver=None):
    """Fence hooks resolving source host/pid per plan.

    `pid_resolver(plan)` returns `(host, pid)`; defaults to reading
    `plan`/`job` attributes populated by the orchestrator loop.
    """
    def _resolve(plan):
        if pid_resolver is not None:
            return pid_resolver(plan)
        host = getattr(plan, "source_host", None)
        pid = getattr(plan, "pid", None)
        return host, pid

    def _terminate(plan):
        host, pid = _resolve(plan)
        if not host or not pid:
            raise RuntimeError("fence terminate: no source host/pid on plan")
        kill = "sudo kill -9" if sudo_kill else "kill -9"
        transport_factory(host).run_command(f"{kill} {int(pid)}", timeout=30.0)

    def _verify(plan):
        host, pid = _resolve(plan)
        if not host or not pid:
            raise RuntimeError("fence verify: no source host/pid on plan")
        res = transport_factory(host).run_command(f"ps -p {int(pid)}", timeout=30.0)
        rc = int(res.get("returncode", 0))
        if rc not in (0, 1):
            raise RuntimeError("fencing verification transport is uncertain")
        return rc == 1

    def _invalidate(plan):
        return True

    return {"invalidate": _invalidate, "terminate": _terminate, "verify": _verify}


def _checkpoint_snapshot(images):
    import json
    path = Path(images) / "appstate" / "progress.json"
    if not path.is_file():
        return None
    state = json.loads(path.read_text())
    value = state.get("progress", state.get("tick", state.get("iterations_done")))
    return {"progress_delta": value} if value is not None else None
