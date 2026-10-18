"""Activation requires a fresh target identity, not merely SIGCONT receipt."""
from datetime import datetime, timezone
import json
from types import SimpleNamespace

from orchestrator.worker_execution import WorkerExecution
from orchestrator.migration_coordinator import MigrationCoordinator, MigrationFailed
from orchestrator.policy_engine import MigrationRegime
import pytest


def test_controller_activation_waits_for_target_heartbeat():
    commands = []

    class Transport:
        def run_command(self, command, timeout=60):
            return {"returncode": 0, "stdout": json.dumps({
                "job_id": "j", "instance_id": "i-target", "execution_epoch": 1,
                "pid": 42, "observed_at": datetime.now(timezone.utc).isoformat()}),
                "stderr": ""}

    def channel(command):
        commands.append(command)
        return {"accepted": True}

    worker = WorkerExecution(lambda host: Transport(), controller_channel_factory=lambda _: channel,
                             lease_proof=lambda: {"owner": "leader", "generation": 2})
    state = SimpleNamespace(plan=SimpleNamespace(plan_id="p", job_id="j"), target_host="target",
                            target_instance_id="i-target", expected_epoch=1, target_pid=42,
                            target_process_start="start")
    worker.activate(state)
    assert commands == [{"operation_id": "p:activate", "action": "activate",
        "instance_id": "i-target", "job_id": "j", "expected_execution_epoch": 1,
        "pid": 42, "process_start": "start", "lease": {"owner": "leader", "generation": 2}}]


def test_fence_gate_rechecks_target_containment():
    coordinator = MigrationCoordinator(None, None, None, None, None, None,
        target_containment_probe=lambda state: False)
    coordinator._execution_state = SimpleNamespace(expected_epoch=0, fencing_confirmed=False)
    plan = SimpleNamespace(job_id="j", regime=MigrationRegime.ARBITRAGE,
                           is_expired=lambda: False)
    with pytest.raises(MigrationFailed, match="not a contained"):
        coordinator._final_gate(plan, "FENCE")


def test_source_resume_requires_original_process_running():
    commands = []

    class Transport:
        def run_command(self, command, timeout=60):
            commands.append(command)
            if " inspect " in command:
                return {"returncode": 0, "stdout": json.dumps(
                    {"same_process": True, "contained": False})}
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
                return {"returncode": 0, "stdout": json.dumps({
                    "job_id": "j", "instance_id": "i-target", "execution_epoch": 1,
                    "pid": 42, "observed_at": datetime.now(timezone.utc).isoformat()})}
            return {"returncode": 0, "stdout": "", "stderr": ""}

    state = SimpleNamespace(plan=SimpleNamespace(job_id="j"), target_host="target",
                            target_instance_id="i-target", expected_epoch=1,
                            target_pid=42, target_process_start="start")
    WorkerExecution(lambda host: Transport()).activate(state)
    assert commands[0].startswith("sudo -n mv ")
    assert "sudo -n python3 -I /opt/spot-worker-control/control_entry.py" in commands[0]
