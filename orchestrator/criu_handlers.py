# orchestrator/criu_handlers.py — CRIU dump/restore over a WorkerTransport.
"""Builds CheckpointManager handlers from the `criu_wrapper.sh` exit-code
contract (Protocols #9 §11.8). Exit codes are the API: 0 ok, 1 criu error,
2 timeout, 3 preflight/PARTIAL, 4 incompatible environment.
"""
import shlex
import time

from orchestrator.checkpoint_manager import CheckpointResult, RestoreResult

WRAPPER_PATH = "/opt/job_workspace/checkpoint/criu_wrapper.sh"


def _sh(command: str) -> str:
    return command


def make_dump_handler(transport, wrapper_path: str = WRAPPER_PATH,
                      workspace_root: str = "/opt/job_workspace"):
    def _dump(*, job_id: str, pid: int, host=None, timeout: float = 280.0):
        images_dir = f"{workspace_root}/checkpoint"
        cmd = (f"sudo bash {shlex.quote(wrapper_path)} dump "
               f"{int(pid)} {shlex.quote(images_dir)}")
        started = time.monotonic()
        try:
            res = transport.run_command(cmd, timeout=timeout)
        except Exception as exc:
            raise RuntimeError(f"dump transport failed for {job_id}: {exc}")
        rc = int(res.get("returncode", 1))
        if rc == 2:
            raise TimeoutError(f"dump timed out for {job_id}")
        if rc == 3:
            raise RuntimeError(f"dump preflight failed for {job_id}: "
                               f"{res.get('stderr', '')}")
        if rc != 0:
            raise RuntimeError(f"CRIU_DUMP_FAILED for {job_id}: "
                               f"{res.get('stderr', '')}")
        return CheckpointResult(
            checkpoint_id=f"chk-{job_id}-{int(time.time())}",
            size_bytes=0, duration_seconds=time.monotonic() - started,
            digest="", status="LOCAL", job_id=job_id, lineage_id=job_id)
    return _dump


def make_restore_handler(transport, wrapper_path: str = WRAPPER_PATH,
                         workspace_root: str = "/opt/job_workspace"):
    def _restore(*, checkpoint_id: str, host=None, timeout: float = 170.0):
        images_dir = f"{workspace_root}/checkpoint"
        cmd = (f"sudo bash {shlex.quote(wrapper_path)} restore "
               f"{shlex.quote(images_dir)}")
        started = time.monotonic()
        try:
            res = transport.run_command(cmd, timeout=timeout)
        except Exception:
            return RestoreResult(checkpoint_id=checkpoint_id, success=False,
                                 duration_seconds=time.monotonic() - started,
                                 outcome="UNKNOWN")
        rc = int(res.get("returncode", 1))
        duration = time.monotonic() - started
        if rc == 2:
            return RestoreResult(checkpoint_id=checkpoint_id, success=False,
                                 duration_seconds=duration, outcome="UNKNOWN")
        if rc == 3:
            return RestoreResult(checkpoint_id=checkpoint_id, success=False,
                                 duration_seconds=duration,
                                 outcome="PARTIAL_RESTORE")
        if rc == 4:
            return RestoreResult(checkpoint_id=checkpoint_id, success=False,
                                 duration_seconds=duration, outcome="FAILED")
        if rc != 0:
            return RestoreResult(checkpoint_id=checkpoint_id, success=False,
                                 duration_seconds=duration, outcome="FAILED")
        out = (res.get("stdout", "") or "").strip().split()
        pid = int(out[-1]) if out and out[-1].isdigit() else None
        return RestoreResult(checkpoint_id=checkpoint_id, success=True,
                             duration_seconds=duration, process_id=pid,
                             outcome="SUCCEEDED")
    return _restore


def make_verify_handler(transport, wrapper_path: str = WRAPPER_PATH,
                        workspace_root: str = "/opt/job_workspace"):
    def _verify(*, checkpoint_dir: str = None, timeout: float = 60.0) -> str:
        images_dir = checkpoint_dir or f"{workspace_root}/checkpoint"
        cmd = (f"sudo bash {shlex.quote(wrapper_path)} verify "
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
        return int(res.get("returncode", 0)) != 0

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
    def _dump(*, job_id: str, pid: int, host=None, timeout: float = 280.0):
        transport = _transport_for_host(transport_factory, host)
        return make_dump_handler(transport, wrapper_path, workspace_root)(
            job_id=job_id, pid=pid, host=host, timeout=timeout)
    return _dump


def make_restore_handler_for(transport_factory, wrapper_path: str = WRAPPER_PATH,
                             workspace_root: str = "/opt/job_workspace"):
    """Restore handler resolving the worker transport per call host."""
    def _restore(*, checkpoint_id: str, host=None, timeout: float = 170.0):
        transport = _transport_for_host(transport_factory, host)
        return make_restore_handler(transport, wrapper_path, workspace_root)(
            checkpoint_id=checkpoint_id, host=host, timeout=timeout)
    return _restore


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
        return int(res.get("returncode", 0)) != 0

    def _invalidate(plan):
        return True

    return {"invalidate": _invalidate, "terminate": _terminate, "verify": _verify}
