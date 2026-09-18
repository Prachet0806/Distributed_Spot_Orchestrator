"""Phase 5: worker workloads, hooks, transports, CRIU handler mapping."""
import json
import threading

from worker.jobs.fixture import run as fixture_run
from worker.jobs.monte_carlo import run as mc_run
from worker.checkpoint_hooks import pre_checkpoint, post_restore
from worker.transport import (
    EmulatedWorkerTransport, SSHWorkerTransport, emu_trust_mode,
)
from orchestrator import criu_handlers as handlers


def test_fixture_progress_and_resume(tmp_path):
    prog = str(tmp_path / "progress.json")
    r1 = fixture_run(total_ticks=200, progress_path=prog, sleep_per_tick=0)
    assert r1["tick"] == 200 and r1["interrupted"] is False
    # Resume continues; deterministic accumulator across restart.
    r2 = fixture_run(total_ticks=300, progress_path=prog, sleep_per_tick=0)
    assert r2["tick"] == 300
    assert r2["accumulator"] == fixture_run(
        total_ticks=300, progress_path=None, sleep_per_tick=0)["accumulator"]


def test_fixture_interrupt_persists(tmp_path):
    prog = str(tmp_path / "progress.json")
    stop = threading.Event()
    stop.set()
    r = fixture_run(total_ticks=10000, progress_path=prog, stop_event=stop,
                    sleep_per_tick=0)
    assert r["interrupted"] is True
    with open(prog) as f:
        assert json.load(f)["tick"] == r["tick"]


def test_monte_carlo_progress_resume_and_interrupt(tmp_path):
    prog = str(tmp_path / "mc.json")
    r1 = mc_run(iterations=2000, progress_path=prog, persist_every=500, seed=7)
    assert r1["iterations_done"] == 2000 and r1["interrupted"] is False
    assert 3.0 < r1["pi"] < 3.3
    # Already complete → short-circuits.
    r2 = mc_run(iterations=2000, progress_path=prog, seed=7)
    assert r2["iterations_done"] == 2000 and r2["pi"] == r1["pi"]
    # Interrupt persists partial progress.
    prog2 = str(tmp_path / "mc2.json")
    stop = threading.Event()
    stop.set()
    r3 = mc_run(iterations=10_000_000, progress_path=prog2, stop_event=stop)
    assert r3["interrupted"] is True and r3["pi"] is None
    with open(prog2) as f:
        assert json.load(f)["iterations_done"] == r3["iterations_done"]


def test_hooks_fsync_and_resume_tolerance(tmp_path):
    prog = str(tmp_path / "p.json")
    prog_path = str(prog)
    with open(prog_path, "w") as f:
        json.dump({"tick": 1}, f)
    assert pre_checkpoint(prog_path) is True
    assert post_restore(prog_path) is True
    assert post_restore(str(tmp_path / "missing.json")) is True
    assert pre_checkpoint(None) is True


def test_emulated_transport_runs_locally():
    t = EmulatedWorkerTransport()
    res = t.run_command("echo hello")
    assert res["returncode"] == 0 and "hello" in res["stdout"]
    assert t.close() is None


def test_emu_trust_defaults_off(monkeypatch):
    monkeypatch.delenv("EMU_TRUST_MODE", raising=False)
    assert emu_trust_mode() is False
    assert SSHWorkerTransport("h").emu_trust is False


class _FakeTransport:
    def __init__(self, rc=0, stdout="", stderr=""):
        self.rc, self.stdout, self.stderr = rc, stdout, stderr
        self.commands = []

    def run_command(self, command, timeout=60.0):
        self.commands.append(command)
        return {"returncode": self.rc, "stdout": self.stdout,
                "stderr": self.stderr}

    def close(self):
        pass


def test_dump_handler_success_and_failures():
    ok = handlers.make_dump_handler(_FakeTransport(rc=0))
    res = ok(job_id="j", pid=123)
    assert res.status == "LOCAL" and res.job_id == "j"

    import pytest
    with pytest.raises(RuntimeError):
        handlers.make_dump_handler(_FakeTransport(rc=1, stderr="boom"))(
            job_id="j", pid=1)
    with pytest.raises(TimeoutError):
        handlers.make_dump_handler(_FakeTransport(rc=2))(
            job_id="j", pid=1)
    with pytest.raises(RuntimeError):
        handlers.make_dump_handler(_FakeTransport(rc=3))(
            job_id="j", pid=1)


def test_restore_handler_outcome_mapping():
    mk = handlers.make_restore_handler
    assert mk(_FakeTransport(rc=0, stdout="12345"))(
        checkpoint_id="c").outcome == "SUCCEEDED"
    r = mk(_FakeTransport(rc=0, stdout="12345"))(checkpoint_id="c")
    assert r.process_id == 12345 and r.success is True
    assert mk(_FakeTransport(rc=2))(checkpoint_id="c").outcome == "UNKNOWN"
    assert mk(_FakeTransport(rc=3))(checkpoint_id="c").outcome == "PARTIAL_RESTORE"
    assert mk(_FakeTransport(rc=1))(checkpoint_id="c").outcome == "FAILED"


def test_verify_and_fence_hooks():
    assert handlers.make_verify_handler(_FakeTransport(rc=0))() == "VALID"
    assert handlers.make_verify_handler(_FakeTransport(rc=1))() == "INVALID"
    assert handlers.make_verify_handler(_FakeTransport(rc=9))() == "INDETERMINATE"

    hooks = handlers.make_fence_hooks(_FakeTransport(rc=1))  # ps nonzero = gone
    plan = type("P", (), {"pid": 4242})()
    hooks["terminate"](plan)
    assert hooks["verify"](plan) is True

    alive = handlers.make_fence_hooks(_FakeTransport(rc=0))
    assert alive["verify"](plan) is False


def test_wrapper_contract_strings():
    text = open("checkpoint/criu_wrapper.sh").read()
    for token in ["dump", "restore", "verify", "preflight",
                  "WORKSPACE_ROOT", "PARTIAL", "inventory.img"]:
        assert token in text
