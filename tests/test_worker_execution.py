"""Activation requires a fresh target identity, not merely SIGCONT receipt."""

import hashlib
import json
import shlex
import shutil
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from orchestrator.migration_coordinator import (
    MigrationCoordinator,
    MigrationFailed,
    MigrationState,
)
from orchestrator.policy_engine import MigrationRegime
from orchestrator.worker_execution import CheckpointDataPath, WorkerExecution
from worker import artifact_verify


def test_controller_activation_waits_for_target_heartbeat():
    commands = []

    class Transport:
        def run_command(self, command, timeout=60):
            return {
                "returncode": 0,
                "stdout": json.dumps(
                    {
                        "job_id": "j",
                        "instance_id": "i-target",
                        "execution_epoch": 1,
                        "pid": 42,
                        "observed_at": datetime.now(timezone.utc).isoformat(),
                    }
                ),
                "stderr": "",
            }

    def channel(command):
        commands.append(command)
        return {"accepted": True}

    worker = WorkerExecution(
        lambda host: Transport(),
        controller_channel_factory=lambda _: channel,
        lease_proof=lambda: {"owner": "leader", "generation": 2},
    )
    state = SimpleNamespace(
        plan=SimpleNamespace(plan_id="p", job_id="j"),
        target_host="target",
        target_instance_id="i-target",
        expected_epoch=1,
        target_pid=42,
        target_process_start="start",
    )
    worker.activate(state)
    assert commands == [
        {
            "operation_id": "p:activate",
            "action": "activate",
            "instance_id": "i-target",
            "job_id": "j",
            "expected_execution_epoch": 1,
            "pid": 42,
            "process_start": "start",
            "lease": {"owner": "leader", "generation": 2},
        }
    ]


def test_fence_gate_rechecks_target_containment():
    coordinator = MigrationCoordinator(
        None, None, None, None, None, None, target_containment_probe=lambda state: False
    )
    coordinator._execution_state = SimpleNamespace(
        expected_epoch=0, fencing_confirmed=False
    )
    plan = SimpleNamespace(
        job_id="j", regime=MigrationRegime.ARBITRAGE, is_expired=lambda: False
    )
    with pytest.raises(MigrationFailed, match="not a contained"):
        coordinator._final_gate(plan, "FENCE")


def test_source_resume_requires_original_process_running():
    commands = []

    class Transport:
        def run_command(self, command, timeout=60):
            commands.append(command)
            if " inspect " in command:
                return {
                    "returncode": 0,
                    "stdout": json.dumps({"same_process": True, "contained": False}),
                }
            return {"returncode": 0, "stdout": ""}

    plan = SimpleNamespace(source_host="source", pid=73, source_process_start="start")
    WorkerExecution(lambda host: Transport()).resume_source(plan)
    assert " activate " in commands[0]
    assert " inspect " in commands[1]
    assert all("sudo -n python3 -I" in command for command in commands)


def test_direct_activation_uses_privileged_fixed_control_path():
    commands = []

    class Transport:
        def send_file(self, local, remote):
            pass

        def run_command(self, command, timeout=60):
            commands.append(command)
            if command.startswith("cat "):
                return {
                    "returncode": 0,
                    "stdout": json.dumps(
                        {
                            "job_id": "j",
                            "instance_id": "i-target",
                            "execution_epoch": 1,
                            "pid": 42,
                            "observed_at": datetime.now(timezone.utc).isoformat(),
                        }
                    ),
                }
            return {"returncode": 0, "stdout": "", "stderr": ""}

    state = SimpleNamespace(
        plan=SimpleNamespace(job_id="j"),
        target_host="target",
        target_instance_id="i-target",
        expected_epoch=1,
        target_pid=42,
        target_process_start="start",
    )
    WorkerExecution(lambda host: Transport()).activate(state)
    assert commands[0].startswith("sudo -n mv ")
    assert "sudo -n python3 -I /opt/spot-worker-control/control_entry.py" in commands[0]


def test_target_transfer_replays_after_lost_commit_response(tmp_path):
    workspace = tmp_path / "worker"
    workspace.mkdir()
    payload = b"checkpoint-image"
    digest = hashlib.sha256(payload).hexdigest()
    manifest = {
        "job_id": "chk-1",
        "files": {"inventory.img": {"sha256": digest, "size_bytes": len(payload)}},
        "artifact": {"sha256": "archive-digest", "size_bytes": 42},
    }

    class Storage:
        downloads = 0

        def download(self, checkpoint_id, dst, expected_checksum):
            assert expected_checksum == "archive-digest"
            self.downloads += 1
            path = __import__("pathlib").Path(dst)
            path.mkdir()
            (path / "inventory.img").write_bytes(payload)
            return manifest

    class Transport:
        lose_commit_response = True

        def run_command(self, command, timeout=60):
            words = shlex.split(command)
            index = words.index("verify-artifact")
            action = words[index + 1]
            args = dict(zip(words[index + 2 :: 2], words[index + 3 :: 2]))
            result = {
                "prepare": artifact_verify.prepare,
                "commit": artifact_verify.commit,
                "status": artifact_verify.status,
            }[action](
                args["--workspace"],
                args["--checkpoint-id"],
                args["--operation-id"],
                args["--checksum"],
            )
            if action == "commit" and self.lose_commit_response:
                self.lose_commit_response = False
                raise ConnectionError("response lost after atomic promotion")
            return {"returncode": 0, "stdout": json.dumps(result)}

        def send_directory(self, local, remote):
            shutil.copytree(local, remote)

        def send_file(self, local, remote):
            shutil.copyfile(local, remote)

    storage = Storage()
    transport = Transport()
    checkpoint_store = SimpleNamespace(get=lambda _: {"checksum": "archive-digest"})
    path = CheckpointDataPath(
        lambda host: transport, storage, checkpoint_store, workspace=str(workspace)
    )
    with pytest.raises(ConnectionError, match="response lost"):
        path.download_to_worker("chk-1", "worker-host", operation_id="op-1")
    assert (
        path.get_operation_status("op-1", checkpoint_id="chk-1", host="worker-host")[
            "state"
        ]
        == "SUCCEEDED"
    )
    result = path.download_to_worker("chk-1", "worker-host", operation_id="op-1")
    assert result["bytes_transferred"] == 42
    assert storage.downloads == 1
    assert (
        workspace / "checkpoints" / "chk-1" / "inventory.img"
    ).read_bytes() == payload
    (workspace / "checkpoints" / "chk-1" / "inventory.img").write_bytes(b"corrupt")
    assert (
        path.get_operation_status("op-1", checkpoint_id="chk-1", host="worker-host")[
            "state"
        ]
        == "UNKNOWN"
    )


def test_emergency_parallel_work_keeps_lifecycle_on_coordinator_thread():
    barrier = threading.Barrier(2)
    coordinator = MigrationCoordinator(None, None, None, None, None, None)
    coordinator._execution_state = SimpleNamespace(
        plan=SimpleNamespace(migration_id="m1"),
        current_state=MigrationState.PRECHECKING,
        state_entered_at=None,
    )
    coordinator._execute_checkpointing = lambda: barrier.wait(timeout=2)
    coordinator._execute_persisting = lambda: None
    coordinator._execute_provisioning = lambda: barrier.wait(timeout=2)
    owner = threading.get_ident()
    changes = []
    coordinator._execute_emergency_preparation(
        coordinator._execution_state.plan,
        lambda before, after: changes.append(
            (before.value, after.value, threading.get_ident())
        ),
    )
    assert [(before, after) for before, after, _ in changes] == [
        ("PRECHECKING", "CHECKPOINTING"),
        ("CHECKPOINTING", "PERSISTING"),
        ("PERSISTING", "PROVISIONING"),
    ]
    assert all(thread_id == owner for _, _, thread_id in changes)


def test_lost_transport_response_resolves_before_failure():
    coordinator = MigrationCoordinator(None, None, None, None, None, None)

    def lost_response():
        raise ConnectionError("worker committed but SSH response disappeared")

    result = coordinator._run_with_timeout(
        lost_response,
        timeout=1,
        op_id="op-1",
        probe=lambda operation_id: {
            "operation_id": operation_id,
            "state": "SUCCEEDED",
            "result": {"checkpoint_id": "chk-1"},
        },
    )
    assert result == {"checkpoint_id": "chk-1"}
