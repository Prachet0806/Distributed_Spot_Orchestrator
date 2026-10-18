"""A lost restore response cannot issue a second CRIU restore."""
import json
import hashlib
import subprocess

import pytest

from worker.restore_guard import WRAPPER, restore_status, run_restore
from orchestrator.criu_handlers import make_restore_status_provider_for


def local_identity(tmp_path):
    path = tmp_path / "identity.json"
    path.write_text('{"provider":"local"}')
    path.chmod(0o600)
    return path


def test_restore_receipt_replay_uses_original_contained_process(tmp_path):
    images_root = tmp_path / "images"
    images = images_root / "chk-1"
    images.mkdir(parents=True)
    journal = tmp_path / "journal"
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        (images / "restore.receipt.json").write_text(json.dumps({
            "operation_id": "op-1", "process_id": 73, "process_start": "900",
            "contained": True}))
        return subprocess.CompletedProcess(command, 0, "73\n", "")

    identity = lambda pid: {"process_start": "900", "state": "T"}
    args = {"journal_root": journal, "images_root": images_root,
            "identity_lookup": identity, "identity_path": local_identity(tmp_path)}
    first = run_restore("op-1", images, runner=runner, **args)
    second = run_restore("op-1", images, runner=runner, **args)
    assert first["state"] == second["state"] == "SUCCEEDED"
    assert second["result"]["process_id"] == 73
    assert len(calls) == 1


def test_issued_restore_without_receipt_is_unknown_and_never_retried(tmp_path):
    images_root = tmp_path / "images"
    images = images_root / "chk-1"
    images.mkdir(parents=True)
    journal = tmp_path / "journal"
    calls = []

    def timeout(command, **kwargs):
        calls.append(command)
        raise subprocess.TimeoutExpired(command, 190)

    args = {"journal_root": journal, "images_root": images_root,
            "identity_lookup": lambda pid: None, "identity_path": local_identity(tmp_path)}
    assert run_restore("op-1", images, runner=timeout, **args)["state"] == "UNKNOWN"
    assert run_restore("op-1", images, runner=timeout, **args)["state"] == "UNKNOWN"
    assert len(calls) == 1
    with pytest.raises(RuntimeError, match="request mismatch"):
        restore_status("op-1", images_root / "chk-2",
                       journal_root=journal, images_root=images_root,
                       identity_lookup=lambda pid: None)


def test_restore_status_requires_same_stopped_pid(tmp_path):
    images_root = tmp_path / "images"
    images = images_root / "chk-1"
    images.mkdir(parents=True)
    journal = tmp_path / "journal"

    def runner(command, **kwargs):
        (images / "restore.receipt.json").write_text(json.dumps({
            "operation_id": "op-1", "process_id": 73, "process_start": "900"}))
        return subprocess.CompletedProcess(command, 0, "", "")

    args = {"journal_root": journal, "images_root": images_root}
    run_restore("op-1", images, runner=runner,
                identity_lookup=lambda pid: {"process_start": "900", "state": "T"},
                identity_path=local_identity(tmp_path), **args)
    assert restore_status("op-1", images,
        identity_lookup=lambda pid: {"process_start": "901", "state": "T"}, **args)["state"] == "UNKNOWN"
    assert restore_status("op-1", images,
        identity_lookup=lambda pid: {"process_start": "900", "state": "R"}, **args)["state"] == "UNKNOWN"


def test_aws_restore_revalidates_and_pins_script_before_criu(tmp_path, monkeypatch):
    images_root = tmp_path / "images"
    images = images_root / "chk-1"
    appstate = images / "appstate"
    appstate.mkdir(parents=True)
    script = appstate / "workload.py"
    script.write_text("print('reviewed')\n")
    identity = tmp_path / "identity.json"
    identity.write_text('{"provider":"aws"}')
    identity.chmod(0o600)
    approved = "sha256:" + hashlib.sha256(script.read_bytes()).hexdigest()
    calls = []
    monkeypatch.setattr("worker.restore_guard.validate_trusted_digest",
                        lambda digest: calls.append(("approved", digest)))
    monkeypatch.setattr("worker.restore_guard.stage_trusted_script",
                        lambda path, digest: calls.append(("staged", path, digest)))

    def failed_criu(command, **kwargs):
        calls.append(("criu", command))
        return subprocess.CompletedProcess(command, 1, "", "restore failed")

    args = {"journal_root": tmp_path / "journal", "images_root": images_root,
            "identity_path": identity, "runner": failed_criu}
    assert run_restore("op-1", images, **args)["state"] == "FAILED"
    assert calls == [("approved", approved), ("staged", script, approved),
                     ("criu", ["bash", str(WRAPPER), "restore", str(images)])]
    assert run_restore("op-1", images, **args)["state"] == "FAILED"
    assert len(calls) == 3


def test_remote_status_provider_uses_durable_plan_context():
    class Operations:
        def get(self, operation_id):
            return {"request": {"step_name": "restoring", "plan_id": "p"}}

    class Plans:
        def get_plan(self, plan_id):
            return {"execution_context": {"target_host": "target", "checkpoint_id": "chk-1"}}

    class Transport:
        def run_command(self, command, timeout=30):
            assert "restore status" in command
            assert "chk-1" in command
            return {"returncode": 0, "stdout": '{"state":"SUCCEEDED"}'}

    provider = make_restore_status_provider_for(lambda host: Transport(), Operations(), Plans())
    assert provider("op-1")["state"] == "SUCCEEDED"
