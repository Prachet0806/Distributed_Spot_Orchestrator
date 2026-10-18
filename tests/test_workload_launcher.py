"""User workload settings cannot override the worker control identity."""
import hashlib
import os

import pytest

from worker.workload_launcher import WorkloadLauncher, stage_trusted_script, validate_trusted_digest


@pytest.mark.parametrize("reserved", ["WORKER_PROVIDER", "WORKLOAD_EXECUTION_EPOCH", "INSTANCE_ID", "AWS_ACCESS_KEY_ID"])
def test_reserved_environment_is_rejected_before_process_launch(tmp_path, reserved, monkeypatch):
    monkeypatch.setenv("WORKER_PROVIDER", "local")
    script = tmp_path / "workload.py"
    script.write_text("pass\n")
    spec = {"operation_id": "op", "job_id": "j", "instance_id": "i-1", "execution_epoch": 0,
            "script": "workload.py", "script_digest": "sha256:" + hashlib.sha256(script.read_bytes()).hexdigest(),
            "environment": {reserved: "unsafe"}}
    with pytest.raises(ValueError, match="reserved"):
        WorkloadLauncher(tmp_path).launch(spec)
    assert not (tmp_path / "workload.log").exists()


def test_aws_workload_policy_denies_unapproved_digest(tmp_path):
    policy = tmp_path / "allowed.json"
    policy.write_text('{"schema":"trusted-scripts/v1","allowed_sha256":["sha256:approved"]}')
    validate_trusted_digest("sha256:approved", policy, enforce_owner=False)
    with pytest.raises(PermissionError, match="not approved"):
        validate_trusted_digest("sha256:other", policy, enforce_owner=False)


def test_launch_does_not_inherit_controller_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKER_PROVIDER", "local")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "controller-only-secret")
    script = tmp_path / "workload.py"
    script.write_text("pass\n")
    captured = {}

    class Process:
        pid = os.getpid()

    def fake_popen(command, **options):
        captured.update({"command": command, **options})
        return Process()

    monkeypatch.setattr("worker.workload_launcher.subprocess.Popen", fake_popen)
    spec = {"operation_id": "launch-1", "job_id": "j", "instance_id": "i-1",
            "execution_epoch": 0, "script": "workload.py",
            "script_digest": "sha256:" + hashlib.sha256(script.read_bytes()).hexdigest(),
            "environment": {"EXPERIMENT": "42"}}
    WorkloadLauncher(tmp_path).launch(spec)
    assert "-I" in captured["command"]
    assert captured["env"]["EXPERIMENT"] == "42"
    assert "AWS_SECRET_ACCESS_KEY" not in captured["env"]


@pytest.mark.skipif(os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() != 0,
                    reason="root-owned staging requires a root Linux test host")
def test_trusted_script_staging_pins_reviewed_bytes(tmp_path):
    script = tmp_path / "workload.py"
    script.write_text("print('reviewed')\n")
    approved = "sha256:" + hashlib.sha256(script.read_bytes()).hexdigest()
    stage = tmp_path / "staged"
    stage.mkdir()
    target = stage_trusted_script(script, approved, root=stage)
    script.write_text("print('changed')\n")
    assert target.read_text() == "print('reviewed')\n"
    with pytest.raises(ValueError, match="changed"):
        stage_trusted_script(script, approved, root=stage)
    script.unlink()
    script.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        stage_trusted_script(script, approved, root=stage)
