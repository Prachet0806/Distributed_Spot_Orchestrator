# worker/job_runner.py — workload entrypoint: READY, heartbeat, graceful stop.
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# Ensure project root is on sys.path (works on remote worker)
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from worker.jobs.monte_carlo import run as run_monte_carlo
from worker.jobs.fixture import run as run_fixture
from worker.spot_interrupt import monitor_spot_interruption
from worker.checkpoint_hooks import pre_checkpoint, post_restore
from worker.constants import (
    SPOT_INTERRUPT_FLAG,
    READY_FLAG,
    WORKSPACE_ROOT,
    DEFAULT_MONTE_CARLO_ITERATIONS,
)

FLAG_PATH = SPOT_INTERRUPT_FLAG

JOBS = {
    "monte_carlo": run_monte_carlo,
    "fixture": run_fixture,
}


def _write_ready():
    payload = {"ready_at": datetime.now(timezone.utc).isoformat(),
               "pid": os.getpid()}
    Path(READY_FLAG).parent.mkdir(parents=True, exist_ok=True)
    with open(READY_FLAG, "w") as f:
        json.dump(payload, f)


def _heartbeat_payload(progress_path=None):
    """T1 heartbeat/v1 contract (docs/telemetry-contract.md).

    Identity comes from the environment (baked AMI exports these at boot);
    `pid`/`at` keys are kept for backward compatibility with local readers.
    Workload progress rides piggyback when a fresh progress file exists —
    the CPR estimator parses it; absence only lowers confidence downstream.
    """
    payload = {
        "schema": "heartbeat/v1",
        "job_id": os.getenv("WORKLOAD_JOB_ID", "local-job"),
        "instance_id": os.getenv("INSTANCE_ID", "local"),
        "execution_epoch": int(os.getenv("WORKLOAD_EXECUTION_EPOCH", "0")),
        "pid": os.getpid(),
        "at": datetime.now(timezone.utc).isoformat(),
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "workload": {"state": "RUNNING"},
    }
    if progress_path:
        try:
            with open(progress_path) as f:
                snapshot = json.load(f)
            if isinstance(snapshot, dict):
                payload["workload"]["progress_snapshot"] = snapshot
        except Exception:
            pass
    return payload


def _heartbeat(stop_event, interval=15.0, progress_path=None):
    path = os.path.join(WORKSPACE_ROOT, "heartbeat.json")
    if progress_path is None:
        progress_path = os.path.join(WORKSPACE_ROOT, "progress.json")
    while not stop_event.wait(interval):
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w") as f:
                json.dump(_heartbeat_payload(progress_path), f)
        except Exception:
            pass


def main():
    job_name = os.getenv("WORKLOAD_JOB", "monte_carlo")
    job = JOBS.get(job_name, run_monte_carlo)
    iterations = int(os.getenv("WORKLOAD_ITERATIONS",
                               DEFAULT_MONTE_CARLO_ITERATIONS))
    progress_path = os.getenv(
        "WORKLOAD_PROGRESS_PATH",
        os.path.join(WORKSPACE_ROOT, "progress.json"))
    print(f"Job started with PID {os.getpid()} (job={job_name})")

    post_restore(progress_path)

    stop_event = threading.Event()
    monitor_thread = threading.Thread(
        target=monitor_spot_interruption,
        args=(FLAG_PATH,),
        kwargs={"stop_event": stop_event},
        daemon=True,
    )
    monitor_thread.start()
    heartbeat_thread = threading.Thread(
        target=_heartbeat, args=(stop_event,), daemon=True)
    heartbeat_thread.start()

    _write_ready()

    kwargs = {"stop_event": stop_event, "interrupt_flag_path": FLAG_PATH,
              "progress_path": progress_path}
    if job_name == "monte_carlo":
        kwargs["iterations"] = iterations
    elif job_name == "fixture":
        kwargs["total_ticks"] = int(os.getenv("WORKLOAD_TICKS", "2000"))
        kwargs["sleep_per_tick"] = float(os.getenv("WORKLOAD_TICK_SLEEP", "0.001"))
    try:
        result = job(**kwargs)
    finally:
        pre_checkpoint(progress_path)
        stop_event.set()
    print(f"Job finished: {result}")
    return result


if __name__ == "__main__":
    main()
