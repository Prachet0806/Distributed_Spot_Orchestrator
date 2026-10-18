"""New store instances must recover evidence without process-local caches."""
from unittest.mock import Mock
import boto3
import pytest
from moto import mock_aws
from storage.plan_store import PlanStore
from storage.checkpoint_store import CheckpointStore
from storage.audit_store import AuditStore
from storage.pool_registry import PoolRegistryStore
from storage.history_store import HistoryStore
from storage.config_store import ConfigStore
from storage.operation_store import OperationStore
from storage.backend import Documents
from orchestrator.control_plane_lease import ControlPlaneLease, LeaseStore


@pytest.fixture
def resource(monkeypatch):
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "testing")
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        tables = {"spot_arbitrage_plans": "plan_id", "spot_arbitrage_checkpoints": "checkpoint_id",
                  "spot_arbitrage_audit": "audit_id", "spot_arbitrage_candidate_pools": "pool_id",
                  "spot_arbitrage_migration_history": "migration_id", "spot_arbitrage_config": "config_id",
                  "spot_arbitrage_operations": "operation_key", "spot_arbitrage_control_plane_lease": "lease_id"}
        for name, key in tables.items():
            ddb.create_table(TableName=name, KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                             AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
                             BillingMode="PAY_PER_REQUEST")
        yield ddb


def test_plan_restart_completion_and_discovery(resource):
    a = PlanStore(dynamodb_resource=resource)
    a.put_plan_doc({"plan_id": "p", "job_id": "j", "migration_id": "m", "plan_hash": "h",
                    "regime": "ARBITRAGE", "steps": [{"step_id": "s", "type": "CHECKPOINT"}]})
    b = PlanStore(dynamodb_resource=resource)
    assert b.list_active()[0]["plan_id"] == "p"
    assert b.find_preemptible_plan("j", "m")["plan_id"] == "p"
    b.update_step_state("p", "s", "PENDING", "RUNNING", operation_id="op")
    assert a.get_plan("p")["steps"][0]["operation_id"] == "op"
    b.mark_plan_outcome("p", "FAILED")
    assert a.list_active() == []
    assert a.mark_plan_outcome("p", "FAILED")["outcome"] == "FAILED"


def test_checkpoint_restart_metadata_and_locks(resource):
    a = CheckpointStore(dynamodb_resource=resource)
    a.put({"checkpoint_id": "c", "lineage_id": "j", "execution_epoch": 2, "size_bytes": 4})
    a.set_durability("c", "DURABLE")
    a.update("c", integrity_verified=True, artifact_location="s3://b/c", checksum="hash")
    a.lock("c", "m")
    b = CheckpointStore(dynamodb_resource=resource)
    assert b.latest_durable("j")["checksum"] == "hash"
    assert b.get("c")["locks"] == ["m"]
    b.unlock("c", "m")
    assert a.get("c")["locks"] == []


def test_history_audit_config_and_pool_restart(resource):
    HistoryStore(dynamodb_resource=resource).insert({"migration_id": "m", "job_id": "j", "estimated_cost": 1.5})
    b = HistoryStore(dynamodb_resource=resource)
    b.enrich("m", {"actual_cost": 1.2})
    assert HistoryStore(dynamodb_resource=resource).list_by_job("j")[0]["actual_cost"] == 1.2
    AuditStore(dynamodb_resource=resource).append({"audit_id": "a", "job_id": "j", "timestamp": "x", "duration": .5})
    assert AuditStore(dynamodb_resource=resource).list_by_job("j")[0]["duration"] == .5
    ConfigStore(dynamodb_resource=resource).publish("policy", "v3", {"margin": .1})
    assert ConfigStore(dynamodb_resource=resource).resolve({"policy": "v3"}) == {"policy": {"margin": .1}}
    PoolRegistryStore(dynamodb_resource=resource).put_pool("p", {"region": "r"}, 1)
    PoolRegistryStore(dynamodb_resource=resource).put_pool("p", {"region": "r2"}, 2)
    assert PoolRegistryStore(dynamodb_resource=resource).get_pool("p")["version"] == 2


def test_operation_restart_and_request_binding(resource):
    a = OperationStore(dynamodb_resource=resource)
    issued = a.begin("p:provision", {"target": "pool"})
    b = OperationStore(dynamodb_resource=resource)
    assert b.begin("p:provision", {"target": "pool"})["operation_id"] == issued["operation_id"]
    with pytest.raises(RuntimeError, match="different request"):
        b.begin("p:provision", {"target": "other"})
    b.finish(issued["operation_id"], "UNKNOWN")
    a.finish(issued["operation_id"], "SUCCEEDED", result={"instance_id": "i", "duration": .4})
    assert b.get_operation_status(issued["operation_id"])["result"]["instance_id"] == "i"
    with pytest.raises(RuntimeError, match="conflict"):
        b.finish(issued["operation_id"], "FAILED")


def test_durable_failure_never_updates_local_cache():
    table = Mock()
    table.put_item.side_effect = RuntimeError("offline")
    d = Documents("id", "table", Mock(Table=Mock(return_value=table)))
    with pytest.raises(RuntimeError, match="offline"):
        d.put({"id": "j"}, create=True)
    assert d.local == {}


def test_expired_lease_cannot_renew_and_takeover_advances_generation(resource):
    now = [1000.0]
    a = ControlPlaneLease(LeaseStore(dynamodb_resource=resource), owner="a", wall_clock=lambda: now[0])
    b = ControlPlaneLease(LeaseStore(dynamodb_resource=resource), owner="b", wall_clock=lambda: now[0])
    assert a.acquire() and not b.acquire()
    first = a.generation
    now[0] += 46
    assert not a.heartbeat()
    assert b.acquire() and b.generation > first
    a.release()
    b.assert_fresh()


def test_paginated_document_reads():
    table = Mock()
    table.scan.side_effect = [{"Items": [{"id": "a"}], "LastEvaluatedKey": {"id": "a"}},
                              {"Items": [{"id": "b"}]}]
    d = Documents("id", "t", Mock(Table=Mock(return_value=table)))
    assert [r["id"] for r in d.all()] == ["a", "b"]
    assert table.scan.call_args.kwargs["ExclusiveStartKey"] == {"id": "a"}
