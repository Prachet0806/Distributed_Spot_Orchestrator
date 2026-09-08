"""Phase 2: registry transition, stores, event ledger, S3 round-trip."""
import os
import tarfile

import boto3
import pytest
from moto import mock_aws

from storage.dynamo_registry import DynamoRegistry
from storage.plan_store import PlanStore, PlanStepConflictError
from storage.checkpoint_store import CheckpointStore
from storage.audit_store import AuditStore
from storage.pool_registry import PoolRegistryStore
from storage.config_store import ConfigStore
from storage.history_store import HistoryStore
from orchestrator.models_v2 import (
    MigrationPlanV2, PlanStep, PlanStepType, CheckpointRef,
    compute_plan_hash,
)
from orchestrator.event_ledger import EventLedger


@pytest.fixture
def aws_env():
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_SESSION_TOKEN"] = "testing"
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"


@pytest.fixture
def registry(aws_env):
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        dynamodb.create_table(
            TableName="t",
            KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "job_id", "AttributeType": "S"},
                {"AttributeName": "state", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[{
                "IndexName": "StateIndex",
                "KeySchema": [{"AttributeName": "state", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            }],
            BillingMode="PAY_PER_REQUEST",
        )
        yield DynamoRegistry("t", region_name="us-east-1")


def test_transition_normal_bumps_version_not_epoch(registry):
    registry.create("j1", state="RUNNING", region="us-east-1")
    out = registry.transition("j1", "MIGRATING", active_migration_id="m1")
    assert out["version"] == 1
    assert out["execution_epoch"] == 0
    assert out["active_migration_id"] == "m1"


def test_transition_ownership_bumps_both(registry):
    registry.create("j1", state="MIGRATING", region="us-east-1",
                    active_migration_id="m1")
    out = registry.transition("j1", "RUNNING", ownership_change=True,
                              clear_active_migration=True)
    assert out["version"] == 1
    assert out["execution_epoch"] == 1
    assert out["active_migration_id"] is None


def test_transition_rejects_second_active_migration(registry):
    registry.create("j1", state="MIGRATING", region="us-east-1",
                    active_migration_id="m1")
    with pytest.raises(RuntimeError):
        registry.transition("j1", "MIGRATING", active_migration_id="m2")


def test_transition_rejects_stale_version_and_epoch(registry):
    registry.create("j1", state="RUNNING", region="us-east-1")
    registry.transition("j1", "MIGRATING", active_migration_id="m1")
    with pytest.raises(RuntimeError):
        registry.transition("j1", "RUNNING", expected_version=0,
                            clear_active_migration=True)
    with pytest.raises(RuntimeError):
        registry.transition("j1", "RECOVERY_REQUIRED", expected_epoch=99)


def test_transition_rejects_illegal_state(registry):
    registry.create("j1", state="RUNNING", region="us-east-1")
    with pytest.raises(RuntimeError):
        registry.transition("j1", "PLANNED")


def _plan(pid="p1"):
    return MigrationPlanV2(
        plan_id=pid, job_id="j1", migration_id="m1", regime="ARBITRAGE",
        source_pool_id="s", target_pool_id="t",
        created_at="2026-01-01T00:00:00", expires_at="2026-01-01T00:10:00",
        steps=[PlanStep(step_id="a", type=PlanStepType.CHECKPOINT,
                        operation_type="CreateCheckpoint"),
               PlanStep(step_id="b", type=PlanStepType.PROVISION,
                        operation_type="Provision", depends_on=["a"])])


def test_plan_store_immutable_and_cas():
    store = PlanStore()
    plan = _plan()
    plan.plan_hash = compute_plan_hash(plan)
    store.put_plan(plan)
    with pytest.raises(KeyError):
        store.put_plan(plan)
    step = store.update_step_state("p1", "a", "PENDING", "RUNNING",
                                   operation_id="op-1")
    assert step["state"] == "RUNNING"
    with pytest.raises(PlanStepConflictError):
        store.update_step_state("p1", "a", "PENDING", "SUCCEEDED")
    with pytest.raises(PlanStepConflictError):
        store.update_step_state("p1", "a", "RUNNING", "PENDING")
    assert len(store.list_active()) == 1
    store.update_step_state("p1", "a", "RUNNING", "SUCCEEDED")
    store.update_step_state("p1", "b", "PENDING", "SKIPPED")
    assert store.list_active() == []


def test_checkpoint_monotonic_and_gc_locks():
    store = CheckpointStore()
    store.put(CheckpointRef("c1", "lin", 1, 0))
    store.set_durability("c1", "PERSISTING")
    store.set_durability("c1", "DURABLE")
    with pytest.raises(RuntimeError):
        store.set_durability("c1", "LOCAL")
    store.lock("c1", "m1")
    assert store.gc_candidates(keep_n=0, max_age_seconds=0,
                               grace_seconds=0,
                               now="2030-01-01T00:00:00+00:00") == []
    store.unlock("c1", "m1")
    assert store.gc_candidates(keep_n=0, max_age_seconds=0,
                               grace_seconds=0,
                               now="2030-01-01T00:00:00+00:00") == ["c1"]


def test_audit_append_only_and_history_enrich_once():
    audit = AuditStore()
    audit.append({"audit_id": "a1", "job_id": "j", "timestamp": "2026-01-01"})
    with pytest.raises(KeyError):
        audit.append({"audit_id": "a1", "job_id": "j"})
    assert len(audit.list_by_job("j")) == 1

    hist = HistoryStore()
    hist.insert({"migration_id": "m1", "job_id": "j",
                 "estimated_cost": 5.0})
    with pytest.raises(KeyError):
        hist.insert({"migration_id": "m1", "job_id": "j"})
    hist.enrich("m1", {"actual_cost": 4.5})
    with pytest.raises(RuntimeError):
        hist.enrich("m1", {"actual_cost": 4.0})


def test_pool_versions_and_config_pins():
    pools = PoolRegistryStore()
    pools.put_pool("p", {"ami": "ami-1"}, 1)
    with pytest.raises(RuntimeError):
        pools.put_pool("p", {"ami": "ami-2"}, 1)
    cfg = ConfigStore()
    cfg.publish("policy", "v3", {"margin": 0.1})
    assert cfg.resolve({"policy": "v3"}) == {"policy": {"margin": 0.1}}
    with pytest.raises(KeyError):
        cfg.resolve({"policy": "v9"})


def test_event_ledger_dedup_per_consumer():
    ledger = EventLedger()
    assert ledger.record("e1", "c1") is True
    assert ledger.record("e1", "c1") is False
    assert ledger.record("e1", "c2") is True
    ledger.mark_completed("e1", "c1")
    assert ledger.has_processed("e1", "c1") is True
    assert ledger.has_processed("e1", "c2") is False


def test_s3_round_trip_and_checksum_gate(aws_env, tmp_path):
    from storage.s3_manager import S3Manager

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="bkt")
        mgr = S3Manager(bucket="bkt")

        src = tmp_path / "checkpoint"
        src.mkdir()
        (src / "core-1.img").write_bytes(b"core")
        (src / "inventory.img").write_bytes(b"inv")

        mgr.upload("job-9", src=str(src))

        objs = {o["Key"] for o in
                s3.list_objects_v2(Bucket="bkt")["Contents"]}
        assert objs == {"job-9.tar.gz", "job-9.sha256", "job-9.manifest.json"}

        # Archive stores basename(src); dst must share that basename (as in
        # prod where both are CHECKPOINT_DIR).
        dst = tmp_path / "dstws" / "checkpoint"
        mgr.download("job-9", dst=str(dst))
        assert (dst / "core-1.img").read_bytes() == b"core"

        # Corrupt the archive in place → checksum gate must fire, workspace intact.
        key = os.path.join(str(tmp_path), "evil.tar.gz")
        with tarfile.open(key, "w:gz") as tar:
            evil = tmp_path / "evil.txt"
            evil.write_text("x")
            tar.add(str(evil), arcname="evil.txt")
        s3.upload_file(key, "bkt", "job-9.tar.gz")
        with pytest.raises(RuntimeError):
            mgr.download("job-9", dst=str(tmp_path / "restored2" / "checkpoint"))
        assert (dst / "core-1.img").read_bytes() == b"core"  # prior restore untouched


def test_s3_rejects_tar_slip(aws_env, tmp_path):
    from storage.s3_manager import S3Manager, _safe_members

    evil_tar = str(tmp_path / "slip.tar")
    with tarfile.open(evil_tar, "w") as tar:
        info = tarfile.TarInfo(name="../../evil.txt")
        info.size = 3
        import io
        tar.addfile(info, io.BytesIO(b"bad"))
    with tarfile.open(evil_tar) as tar:
        with pytest.raises(RuntimeError):
            list(_safe_members(tar))
