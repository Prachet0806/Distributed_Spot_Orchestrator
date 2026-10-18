"""Run the production loop with explicit, credential-free service wiring."""
from types import SimpleNamespace
import pytest

import boto3
from moto import mock_aws

from orchestrator.local_runtime import create_context
from orchestrator.main import run_iteration, quarantine_restart_frontiers
from orchestrator.control_plane_lease import ControlPlaneLease, LeaseStore
from storage.dynamo_registry import DynamoRegistry
from storage.pool_registry import PoolRegistryStore
from storage.plan_store import PlanStore
from storage.checkpoint_store import CheckpointStore
from storage.memory_registry import MemoryRegistry


def test_production_iteration_preserves_unqualified_pending_job(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(TableName="jobs", KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "job_id", "AttributeType": "S"},
                                  {"AttributeName": "state", "AttributeType": "S"}],
            GlobalSecondaryIndexes=[{"IndexName": "StateIndex", "KeySchema": [
                {"AttributeName": "state", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"}}], BillingMode="PAY_PER_REQUEST")
        for name, key in (("spot_arbitrage_candidate_pools", "pool_id"),
                          ("spot_arbitrage_plans", "plan_id"),
                          ("spot_arbitrage_checkpoints", "checkpoint_id")):
            ddb.create_table(TableName=name, KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST")
        registry = DynamoRegistry("jobs", region_name="us-east-1", dynamodb_resource=ddb)
        registry.create("fixture", state="PENDING", region="us-east-1")

        class Watcher:
            def poll(self):
                return {"us-east-1": {"price": .01, "interruption_probability": .1}}

        def forbidden(*args, **kwargs):
            raise AssertionError("an unqualified pending job must not actuate")

        lease = ControlPlaneLease(LeaseStore())
        assert lease.acquire()
        wiring = dict(registry=registry, watcher=Watcher(), pool_store=PoolRegistryStore(dynamodb_resource=ddb),
            plan_store=PlanStore(dynamodb_resource=ddb), checkpoint_store=CheckpointStore(dynamodb_resource=ddb),
            coordinator_factory=forbidden, provisioner=SimpleNamespace(provision_from_pool=forbidden),
            lease=lease, reconciliation=SimpleNamespace(reconcile=lambda *_: None),
            event_dispatcher=SimpleNamespace(dispatch=forbidden), transport_for=forbidden,
            readiness_evidence_provider=lambda *_: None)
        assert run_iteration(create_context(**wiring, migrate=False)) is not False
        assert registry.get("fixture")["state"] == "PENDING"
        assert run_iteration(create_context(**wiring, migrate=True)) is not False
        assert registry.get("fixture")["state"] == "PENDING"


@pytest.mark.parametrize("report", [
    {"resumed": [{"plan_id": "p", "job_id": "j", "resume_from_type": "RESTORE"}]},
    {"unknown_unresolved": [{"plan_id": "p", "job_id": "j", "step_type": "CHECKPOINT"}]},
    {"orphans": [{"job_id": "j", "migration_id": "m"}]},
])
def test_restart_frontier_is_quarantined_without_reissuing_operations(report):
    from unittest.mock import Mock
    registry = MemoryRegistry()
    registry.create("j", state="RUNNING", region="source")
    registry.transition("j", "MIGRATING", active_migration_id="m")
    plans = PlanStore()
    plans.put_plan_doc({"plan_id": "p", "migration_id": "m", "job_id": "j",
                        "plan_hash": "h", "steps": [], "execution_plan": {"plan_id": "p"}})
    quarantine_restart_frontiers(report, plans, registry, Mock())
    assert registry.get("j")["state"] == "RECONCILIATION_REQUIRED"
    assert plans.get_plan("p")["execution_plan"] == {"plan_id": "p"}
