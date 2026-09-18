# worker/jobs/fixture.py — minimal CRIU-friendly fixture workload.
"""Deterministic counter loop for checkpoint/restore + overhead E2E.

State is a single counter persisted to `progress_path` (JSON) every
`persist_every` ticks and fsynced, so a restored process resumes from
approximately the same execution point. CPU-light by default; set
`work_per_tick` for heavier profiles.
"""
import json
import os
import tempfile
import time


def run(total_ticks=2000, progress_path=None, stop_event=None,
        interrupt_flag_path=None, work_per_tick=0, persist_every=50,
        sleep_per_tick=0.001):
    state = {"tick": 0, "accumulator": 0}
    if progress_path and os.path.exists(progress_path):
        try:
            with open(progress_path) as f:
                loaded = json.load(f)
            state["tick"] = int(loaded.get("tick", 0))
            state["accumulator"] = int(loaded.get("accumulator", 0))
        except Exception:
            pass

    start_tick = state["tick"]
    for tick in range(start_tick, total_ticks):
        if stop_event is not None and stop_event.is_set():
            _persist(progress_path, state)
            return dict(state, interrupted=True)
        if interrupt_flag_path and os.path.exists(interrupt_flag_path):
            _persist(progress_path, state)
            return dict(state, interrupted=True)
        acc = state["accumulator"]
        for _ in range(work_per_tick):
            acc = (acc * 1103515245 + 12345) & 0x7FFFFFFF
        state["accumulator"] = acc
        state["tick"] = tick + 1
        if progress_path and (state["tick"] % persist_every == 0):
            _persist(progress_path, state)
        if sleep_per_tick:
            time.sleep(sleep_per_tick)
    _persist(progress_path, state)
    return dict(state, interrupted=False)


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
