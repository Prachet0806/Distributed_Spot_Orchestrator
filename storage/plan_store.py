# storage/plan_store.py — Plan Store (immutable plans + CAS step advance).
"""Local-memory backend by default; pass a DynamoDB resource for persistence.

Dynamo shape (when wired): table `spot_arbitrage_plans`, PK `plan_id`,
step states in `steps` map; step updates conditional on plan_hash +
expected step state (CAS). Crash recovery reads non-terminal step states.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Optional

from storage.v2_transitions import is_v2_transition_allowed


class PlanExistsError(KeyError):
    pass


class PlanStepConflictError(RuntimeError):
    pass


class PlanStore:
    def __init__(self, table_name: str = "spot_arbitrage_plans", dynamodb_resource=None):
        self.table_name = table_name
        self.dynamodb = dynamodb_resource
        self._plans: dict[str, dict] = {}

    # -- local backend --
    def put_plan(self, plan) -> dict:
        from orchestrator.models_v2 import to_dict, compute_plan_hash
        doc = deepcopy(to_dict(plan))
        pid = doc.get("plan_id")
        if not pid:
            raise ValueError("plan requires plan_id")
        if pid in self._plans:
            raise PlanExistsError(f"plan {pid} already exists (immutable)")
        if not doc.get("plan_hash"):
            # compute from the typed object when available
            try:
                doc["plan_hash"] = compute_plan_hash(plan)
            except Exception:
                doc["plan_hash"] = ""
        # Track B2: optimistic-concurrency counter for step-state CAS.
        # Local and Dynamo advances both bump it; Dynamo writes condition
        # on (plan_hash, version) so concurrent advancers cannot double-
        # advance a step.
        doc.setdefault("version", 0)
        if self.dynamodb is not None:
            table = self.dynamodb.Table(self.table_name)
            table.put_item(
                Item=doc,
                ConditionExpression="attribute_not_exists(plan_id)",
            )
        self._plans[pid] = doc
        return deepcopy(doc)

    def get_plan(self, plan_id: str) -> dict:
        if self.dynamodb is not None:
            table = self.dynamodb.Table(self.table_name)
            resp = table.get_item(Key={"plan_id": plan_id})
            if "Item" not in resp and plan_id not in self._plans:
                raise KeyError(f"plan {plan_id} not found")
            if "Item" in resp:
                return deepcopy(resp["Item"])
        try:
            return deepcopy(self._plans[plan_id])
        except KeyError:
            raise KeyError(f"plan {plan_id} not found")

    def update_step_state(
        self,
        plan_id: str,
        step_id: str,
        expected_from: str,
        to_state: str,
        operation_id: Optional[str] = None,
    ) -> dict:
        doc = self.get_plan(plan_id)
        steps = {s["step_id"]: s for s in doc.get("steps", [])}
        if step_id not in steps:
            raise KeyError(f"step {step_id} not in plan {plan_id}")
        cur = steps[step_id].get("state", "PENDING")
        if cur != expected_from:
            raise PlanStepConflictError(
                f"step {step_id}: expected {expected_from}, found {cur}"
            )
        if not is_v2_transition_allowed("planstep", cur, to_state):
            raise PlanStepConflictError(f"illegal step transition {cur}->{to_state}")
        steps[step_id]["state"] = to_state
        if operation_id is not None:
            steps[step_id]["operation_id"] = operation_id
        doc["steps"] = [steps[s["step_id"]] for s in doc.get("steps", [])]
        expected_version = int(doc.get("version", 0))
        doc["version"] = expected_version + 1
        if self.dynamodb is not None:
            table = self.dynamodb.Table(self.table_name)
            try:
                table.update_item(
                    Key={"plan_id": plan_id},
                    UpdateExpression="SET steps = :steps, version = version + :one",
                    ConditionExpression="plan_hash = :h AND version = :v",
                    ExpressionAttributeValues={
                        ":steps": deepcopy(doc["steps"]),
                        ":h": doc.get("plan_hash"),
                        ":v": expected_version,
                        ":one": 1,
                    },
                )
            except Exception as exc:
                if "ConditionalCheckFailedException" in str(exc):
                    raise PlanStepConflictError(
                        f"step {step_id}: concurrent advance detected "
                        f"(plan_hash/version changed)") from exc
                raise
        stored = self._plans.get(plan_id)
        if stored is not None:
            # Re-check hash integrity on write path.
            if stored.get("plan_hash") != doc.get("plan_hash"):
                raise PlanStepConflictError("plan_hash mismatch: plan mutated")
            stored["steps"] = deepcopy(doc["steps"])
            stored["version"] = doc["version"]
        return deepcopy(steps[step_id])

    def list_active(self) -> list[dict]:
        terminal = {"SUCCEEDED", "FAILED", "SKIPPED"}
        out = []
        for doc in self._plans.values():
            states = {s.get("state", "PENDING") for s in doc.get("steps", [])}
            if states and states <= terminal:
                continue
            out.append(deepcopy(doc))
        return out
