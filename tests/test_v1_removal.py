"""Breaking V1 retirement: only explicit V2 contracts and executors remain."""
import importlib.util
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from orchestrator.checkpoint_manager import CheckpointManager
from orchestrator.migration_coordinator import MigrationCoordinator, MigrationFailed
from orchestrator.migration_history import _normalize_step_result, _step_result_dict
from orchestrator.provisioner import Provisioner, EmulatedProvisioner
from orchestrator.transfer_manager import TransferManager
from orchestrator.validator import Validator, ValidationResult


@pytest.mark.parametrize("module", [
    "orchestrator.migrator", "orchestrator.decision_engine",
    "storage.job_registry", "storage.job_states", "scripts.registry_cli",
])
def test_retired_modules_cannot_be_imported(module):
    assert importlib.util.find_spec(module) is None


@pytest.mark.parametrize("args", [
    ["-m", "orchestrator.main", "--engine", "v1"],
    ["-m", "orchestrator.main", "--registry-path", "old.json"],
    ["scripts/spotctl.py", "--backend", "json", "status", "--name", "j"],
    ["scripts/spotctl.py", "--json-path", "old.json", "status", "--name", "j"],
])
def test_retired_cli_flags_are_rejected_before_aws_access(args):
    result = subprocess.run([sys.executable, *args], capture_output=True, text=True)
    assert result.returncode == 2
    assert "error:" in result.stderr


def test_no_retired_registry_mutation_fallback():
    update = Mock()
    registry = SimpleNamespace(update=update)
    coordinator = MigrationCoordinator(registry, None, None, None, None, None)
    with pytest.raises(MigrationFailed, match="Registry transition"):
        coordinator._job_transition_safe(SimpleNamespace(job_id="j"), "RUNNING")
    update.assert_not_called()


def test_executor_aliases_are_absent():
    checkpoints = CheckpointManager()
    for alias in ("create_checkpoint", "persist_checkpoint", "restore_checkpoint"):
        assert not hasattr(checkpoints, alias)
    assert not hasattr(TransferManager(), "transfer")
    assert not hasattr(Provisioner(), "provision")
    assert not hasattr(Provisioner(), "provision_with_operation")


def test_unprobed_validation_is_inconclusive():
    snapshot = {"cpu_utilization_delta": .5, "memory_utilization_delta": .5,
                "progress_delta": .5}
    report = Validator().validate("m", "j", 1, snapshot, dict(snapshot))
    assert report.overall_result == ValidationResult.PARTIAL
    assert report.to_v2_verdict() == "INCONCLUSIVE"


def test_old_result_and_tolerance_shapes_are_rejected():
    with pytest.raises(TypeError):
        _normalize_step_result('{"size_bytes": 100}')
    with pytest.raises(ValueError):
        _step_result_dict({"result_schema": 0, "result": {}})
    with pytest.raises(ValueError, match="metrics mapping"):
        Validator(default_tolerance={"progress_delta": .1})


def test_real_provisioner_requires_complete_aws_pool():
    with patch("orchestrator.instance_manager.provision_instance") as launch:
        with pytest.raises(ValueError, match="Invalid target pool"):
            Provisioner(key_name="key").provision_from_pool({
                "pool_id": "p", "region": "us-east-1", "instance_type": "t3.micro"})
        launch.assert_not_called()


def test_real_pool_provisioning_replays_same_operation():
    pool = {"pool_id": "p", "provider": "aws", "region": "us-west-2",
            "ami_id": "ami-test", "security_group_id": "sg-test",
            "instance_type": "t3.micro"}
    with patch("orchestrator.instance_manager.provision_instance",
               return_value=("i-worker", "10.0.0.1", "worker.example")) as launch:
        executor = Provisioner(key_name="key")
        first = executor.provision_from_pool(pool, operation_id="op")
        assert executor.provision_from_pool(pool, operation_id="op") is first
        launch.assert_called_once()
        assert launch.call_args.kwargs["region"] == "us-west-2"
        assert launch.call_args.kwargs["idempotency_token"] == "op"


def test_emulation_requires_explicit_executor():
    executor = EmulatedProvisioner()
    first = executor.provision_with_operation("p", operation_id="op")
    assert executor.provision_with_operation("p", operation_id="op") is first
    assert executor.get_operation_status("op")["state"] == "SUCCEEDED"



def test_missing_checkpoint_manifest_is_not_a_compatibility_fallback(tmp_path):
    from storage.s3_manager import S3Manager
    with patch("storage.s3_manager.boto3.client"):
        manager = S3Manager("bucket")
    manager.s3.get_object.side_effect = RuntimeError("manifest missing")
    with pytest.raises(RuntimeError, match="manifest missing"):
        manager.download("job", dst=str(tmp_path / "checkpoint"))
