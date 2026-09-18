# worker/jobs/monte_carlo.py — stateful Pi estimator with durable progress.
"""Progress (`inside`, `iterations_done`) persists to `progress_path` as JSON
(atomic replace + fsync) and resumes on restart, so CRIU restore or cold
restart continues from approximately the same execution point instead of
discarding work. Signature stays backward compatible with job_runner.
"""
import json
import os
import random
import tempfile
import time


def _load_progress(progress_path):
    state = {"inside": 0, "iterations_done": 0}
    if progress_path and os.path.exists(progress_path):
        try:
            with open(progress_path) as f:
                loaded = json.load(f)
            state["inside"] = int(loaded.get("inside", 0))
            state["iterations_done"] = int(loaded.get("iterations_done", 0))
        except Exception:
            pass
    return state


def _persist(progress_path, state):
    if not progress_path:
        return
    directory = os.path.dirname(progress_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory or ".",
                               prefix=".progress-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, progress_path)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def run(iterations=10_000_000, stop_event=None, interrupt_flag_path=None,
        progress_path=None, persist_every=100_000, seed=None):
    if seed is not None:
        random.seed(seed)
    state = _load_progress(progress_path)
    inside = state["inside"]
    start = state["iterations_done"]
    if start >= iterations:
        pi = (inside / iterations) * 4 if iterations else 0.0
        print(f"Estimated Pi = {pi} (already complete)")
        return {"pi": pi, "inside": inside, "iterations_done": start,
                "interrupted": False}

    for i in range(start, iterations):
        if stop_event is not None and stop_event.is_set():
            _persist(progress_path, {"inside": inside, "iterations_done": i})
            print("Spot interruption detected; progress persisted.")
            return {"pi": None, "inside": inside, "iterations_done": i,
                    "interrupted": True}
        if interrupt_flag_path and os.path.exists(interrupt_flag_path):
            _persist(progress_path, {"inside": inside, "iterations_done": i})
            print("Spot interruption flag found; progress persisted.")
            return {"pi": None, "inside": inside, "iterations_done": i,
                    "interrupted": True}
        x, y = random.random(), random.random()
        if x * x + y * y <= 1:
            inside += 1
        if (i + 1) % persist_every == 0:
            _persist(progress_path, {"inside": inside, "iterations_done": i + 1})
        if i % 1_000_000 == 0:
            time.sleep(0.01)
    _persist(progress_path, {"inside": inside, "iterations_done": iterations})
    pi = (inside / iterations) * 4 if iterations else 0.0
    print(f"Estimated Pi = {pi}")
    return {"pi": pi, "inside": inside, "iterations_done": iterations,
            "interrupted": False}
