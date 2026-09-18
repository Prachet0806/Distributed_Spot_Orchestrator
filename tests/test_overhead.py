"""Phase 6c: completion-overhead harness (ADR-022 method, fixture workload).

overhead = (migrated − baseline) / baseline wall-clock. The treatment run
adds durable progress persists plus one interrupt/resume cycle — the same
shape as a checkpointed migration. Completion (tick + accumulator) must be
bit-identical; overhead is reported and gated.
"""
import time

from worker.jobs.fixture import run as fixture_run


def _run_baseline(total_ticks, work_per_tick):
    start = time.perf_counter()
    result = fixture_run(total_ticks=total_ticks, progress_path=None,
                         work_per_tick=work_per_tick, sleep_per_tick=0)
    return time.perf_counter() - start, result


def _run_migrated(total_ticks, work_per_tick, progress_path, persist_every=2000):
    start = time.perf_counter()
    half = total_ticks // 2
    first = fixture_run(total_ticks=half, progress_path=progress_path,
                        work_per_tick=work_per_tick, sleep_per_tick=0,
                        persist_every=persist_every)
    assert first["interrupted"] is False
    # Simulated migration window: checkpoint already persisted above; the
    # resumed process continues from the durable tick (no work repeated).
    resumed = fixture_run(total_ticks=total_ticks, progress_path=progress_path,
                          work_per_tick=work_per_tick,
                          sleep_per_tick=0, persist_every=persist_every)
    return time.perf_counter() - start, resumed


def test_migrated_completion_identical_and_overhead_gated():
    import tempfile
    import os

    total_ticks, work = 60000, 300
    base_dur, base = _run_baseline(total_ticks, work)
    with tempfile.TemporaryDirectory() as tmp:
        prog = os.path.join(tmp, "progress.json")
        mig_dur, migrated = _run_migrated(total_ticks, work, prog)
    assert migrated["tick"] == base["tick"] == total_ticks
    assert migrated["accumulator"] == base["accumulator"]
    assert migrated["interrupted"] is False
    overhead = (mig_dur - base_dur) / max(base_dur, 1e-9)
    print(f"\nbaseline={base_dur:.2f}s migrated={mig_dur:.2f}s overhead={overhead:.3f}")
    assert overhead <= 0.20
