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
    import statistics

    total_ticks, work = 60000, 300
    # Wall-clock on shared runners is noisy (observed 0.11-0.43 for the
    # same code); gate the median of 3 rounds, not a single sample.
    # The 0.20 threshold itself is unchanged (ADR-022).
    rounds = []
    for _ in range(3):
        base_dur, base = _run_baseline(total_ticks, work)
        with tempfile.TemporaryDirectory() as tmp:
            prog = os.path.join(tmp, "progress.json")
            mig_dur, migrated = _run_migrated(total_ticks, work, prog)
        assert migrated["tick"] == base["tick"] == total_ticks
        assert migrated["accumulator"] == base["accumulator"]
        assert migrated["interrupted"] is False
        rounds.append((mig_dur - base_dur) / max(base_dur, 1e-9))
    overhead = statistics.median(rounds)
    print(f"\nrounds={[f'{r:.3f}' for r in rounds]} median_overhead={overhead:.3f}")
    assert overhead <= 0.20
