"""Lost-response probes use the executor that actually performed each step."""

import shutil
from pathlib import Path

from orchestrator.checkpoint_manager import CheckpointManager
from orchestrator.criu_handlers import (
    make_dump_status_provider_for,
    make_persist_status_provider_for,
)
from storage.checkpoint_store import CheckpointStore
from storage.operation_store import OperationStore


def test_dump_completion_marker_reconstructs_checkpoint_and_metadata(tmp_path):
    operations = OperationStore()
    issued = operations.begin(
        "plan:checkpointing",
        {
            "plan_id": "plan",
            "job_id": "job",
            "step_name": "checkpointing",
            "source_host": "source",
        },
    )
    checkpoint_id = "chk-" + issued["operation_id"]
    images = tmp_path / "checkpoints" / checkpoint_id
    images.mkdir(parents=True)
    (images / "dump.complete").write_bytes(b"")
    (images / "inventory.img").write_bytes(b"inventory")

    class Transport:
        def run_command(self, command, timeout):
            return {"returncode": 0 if (images / "dump.complete").exists() else 1}

        def fetch_directory(self, remote, destination):
            assert remote == str(images).replace("\\", "/")
            shutil.copytree(images, destination)

    store = CheckpointStore()
    manager = CheckpointManager(
        checkpoint_store=store,
        status_provider=make_dump_status_provider_for(
            lambda host: Transport(),
            operations,
            workspace_root=str(tmp_path).replace("\\", "/"),
        ),
    )
    report = manager.get_operation_status(issued["operation_id"])
    assert report["state"] == "SUCCEEDED"
    assert report["result"].checkpoint_id == checkpoint_id
    assert Path(store.get(checkpoint_id)["local_path"]).is_dir()


def test_persist_manifest_resolves_lost_metadata_write():
    operations = OperationStore()
    dump = operations.begin(
        "plan:checkpointing", {"plan_id": "plan", "step_name": "checkpointing"}
    )
    persist = operations.begin(
        "plan:persisting", {"plan_id": "plan", "step_name": "persisting"}
    )
    checkpoint_id = "chk-" + dump["operation_id"]
    store = CheckpointStore()
    store.put(
        {
            "checkpoint_id": checkpoint_id,
            "lineage_id": "job",
            "execution_epoch": 0,
            "checksum": "local-digest",
            "durability": "LOCAL",
        }
    )

    class Storage:
        bucket = "bucket"

        def manifest(self, identity):
            assert identity == checkpoint_id
            return {
                "artifact": {
                    "key": f"{identity}.tar.gz",
                    "sha256": "archive-digest",
                    "size_bytes": 128,
                }
            }

    probe = make_persist_status_provider_for(Storage(), store, operations)
    assert probe(persist["operation_id"])["state"] == "SUCCEEDED"
    saved = store.get(checkpoint_id)
    assert saved["durability"] == "DURABLE"
    assert saved["checksum"] == "archive-digest"
    assert probe(persist["operation_id"])["state"] == "SUCCEEDED"
