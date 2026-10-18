"""V2 emulation registry rejects retired states and record shapes."""
import pytest
from storage.memory_registry import MemoryRegistry


def test_v2_create_transition_and_copy_isolation():
    reg = MemoryRegistry()
    reg.create("j", state="RUNNING", region="us-east-1")
    record = reg.get("j")
    record["state"] = "FAILED"
    assert reg.get("j")["state"] == "RUNNING"
    out = reg.transition("j", "MIGRATING", expected_version=0,
                         expected_epoch=0, active_migration_id="m")
    assert out["version"] == 1 and out["execution_epoch"] == 0
    with pytest.raises(RuntimeError, match="Optimistic lock"):
        reg.transition("j", "RUNNING", expected_version=0)
    with pytest.raises(RuntimeError, match="Epoch conflict"):
        reg.transition("j", "RUNNING", expected_epoch=1)
    out = reg.transition("j", "RUNNING", ownership_change=True)
    assert out["execution_epoch"] == 1 and out["active_migration_id"] is None
    assert reg.list_by_state("RUNNING") == [out]


@pytest.mark.parametrize("state", ["CHECKPOINTING", "UPLOADING", "PROVISIONING", "TERMINATED"])
def test_retired_states_rejected(state):
    reg = MemoryRegistry()
    reg.create("j", state="RUNNING")
    with pytest.raises(RuntimeError, match="Invalid state transition"):
        reg.transition("j", state)
    with pytest.raises(ValueError):
        reg.create("old", state=state)


def test_old_record_shapes_rejected():
    with pytest.raises(ValueError, match="missing fields"):
        MemoryRegistry({"j": {"job_id": "j", "state": "RUNNING"}})
    with pytest.raises(ValueError, match="Only V2"):
        MemoryRegistry().create("j", state="RUNNING", engine_version="v1")
    assert not hasattr(MemoryRegistry(), "update")


@pytest.mark.parametrize("counter", [None, "0", -1, True])
def test_v2_counters_are_not_upgraded(counter):
    with pytest.raises(ValueError, match="nonnegative integer"):
        MemoryRegistry().create("j", state="RUNNING", execution_epoch=counter)
