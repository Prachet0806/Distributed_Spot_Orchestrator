import boto3
from botocore.exceptions import ClientError
from boto3.dynamodb.conditions import Attr, Key
from threading import Lock
from datetime import datetime
from storage.job_states import is_transition_allowed
import time
import logging

logger = logging.getLogger(__name__)

# Optional metrics (only imported if available)
try:
    from orchestrator.metrics import get_metrics
    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False
    
    def get_metrics():
        """Stub for when metrics not available"""
        class StubMetrics:
            def inc(self, *args, **kwargs): pass
            def observe(self, *args, **kwargs): pass
            def set_gauge(self, *args, **kwargs): pass
        return StubMetrics()


class DynamoRegistry:
    """
    DynamoDB-backed job registry.

    Table schema (you create it):
      - PK: job_id (S)
      - Attributes: state, region, pid, public_ip, workload_type, version (N), last_updated (S), etc.
    """

    def __init__(self, table_name: str, region_name: str | None = None):
        self.table_name = table_name
        self.dynamodb = boto3.resource("dynamodb", region_name=region_name)
        self.table = self.dynamodb.Table(table_name)
        self.lock = Lock()

    def get(self, job_id: str):
        """Get a job by ID with metrics tracking."""
        start = time.time()
        try:
            resp = self.table.get_item(Key={"job_id": job_id})
            if "Item" not in resp:
                get_metrics().inc("dynamodb_get_notfound_total")
                raise KeyError(f"job_id {job_id} not found")
            get_metrics().inc("dynamodb_get_success_total")
            get_metrics().observe("dynamodb_get_duration_seconds", time.time() - start)
            return resp["Item"]
        except ClientError as e:
            get_metrics().inc("dynamodb_get_error_total")
            raise RuntimeError(f"Dynamo get failed: {e}")

    def create(self, job_id: str, **attrs):
        """Create a new job with metrics tracking."""
        start = time.time()
        item = {
            "job_id": job_id,
            "version": 0,
            "execution_epoch": 0,
            "active_migration_id": None,
            "last_updated": datetime.utcnow().isoformat(),
            **attrs,
        }
        # Explicit V2 identity defaults win over caller only when absent.
        item.setdefault("execution_epoch", 0)
        item.setdefault("active_migration_id", None)
        try:
            self.table.put_item(Item=item, ConditionExpression="attribute_not_exists(job_id)")
            get_metrics().inc("dynamodb_create_success_total")
            get_metrics().observe("dynamodb_create_duration_seconds", time.time() - start)
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                get_metrics().inc("dynamodb_create_conflict_total")
                raise KeyError(f"job_id {job_id} already exists")
            get_metrics().inc("dynamodb_create_error_total")
            raise RuntimeError(f"Dynamo create failed: {e}")

    def _current_version(self, job_id: str):
        try:
            resp = self.table.get_item(Key={"job_id": job_id}, ProjectionExpression="version")
            if "Item" not in resp:
                raise KeyError(f"job_id {job_id} not found")
            return resp["Item"].get("version")
        except ClientError as e:
            raise RuntimeError(f"Dynamo version check failed: {e}")

    def update(self, job_id: str, state: str, expected_version: int | None = None, **attrs):
        """
        Update state and attributes with optimistic locking (version check).
        If expected_version is None, it reads the current version first.
        """
        start = time.time()
        with self.lock:
            current_item = self.get(job_id)
            current_state = current_item.get("state")
            if not is_transition_allowed(current_state, state):
                raise RuntimeError(
                    f"Invalid state transition: {current_state} -> {state} for {job_id}"
                )
            current_version = expected_version
            if current_version is None:
                current_version = current_item.get("version")
                if current_version is None:
                    current_version = self._current_version(job_id)
            new_version = (current_version or 0) + 1

            names = {"#state": "state"}
            values = {
                ":state": state,
                ":version": new_version,
                ":last_updated": datetime.utcnow().isoformat(),
            }
            expr_parts = ["#state = :state", "version = :version", "last_updated = :last_updated"]

            for k, v in attrs.items():
                ph_name = f"#{k}"
                ph_val = f":{k}"
                names[ph_name] = k
                values[ph_val] = v
                expr_parts.append(f"{ph_name} = {ph_val}")

            update_expr = "SET " + ", ".join(expr_parts)
            if current_version is None:
                condition = "attribute_not_exists(version)"
                values[":expected"] = 0
            else:
                condition = "version = :expected"
                values[":expected"] = current_version

            try:
                self.table.update_item(
                    Key={"job_id": job_id},
                    UpdateExpression=update_expr,
                    ExpressionAttributeNames=names,
                    ExpressionAttributeValues=values,
                    ConditionExpression=condition,
                )
                get_metrics().inc("dynamodb_update_success_total")
                get_metrics().observe("dynamodb_update_duration_seconds", time.time() - start)
            except ClientError as e:
                if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                    get_metrics().inc("dynamodb_update_conflict_total")
                    raise RuntimeError(f"Optimistic lock failed for job_id {job_id}")
                get_metrics().inc("dynamodb_update_error_total")
                raise RuntimeError(f"Dynamo update failed: {e}")

    def transition(
        self,
        job_id: str,
        to_state: str,
        expected_version: int | None = None,
        expected_epoch: int | None = None,
        ownership_change: bool = False,
        active_migration_id: str | None = None,
        clear_active_migration: bool = False,
        **attrs,
    ):
        """V2 sole mutation path: legal-transition + CAS(version, epoch).

        - Validates against the V2 job table (`storage/v2_transitions.py`);
          legacy V1 states remain accepted for frozen `--engine v1` rows.
        - Normal mutation: version++, epoch unchanged.
        - Ownership transfer: version++ AND epoch++ atomically.
        - MIGRATING entries enforce at most one active migration per job.
        Returns the updated item.
        """
        from storage.v2_transitions import is_v2_transition_allowed

        start = time.time()
        with self.lock:
            current_item = self.get(job_id)
            current_state = current_item.get("state")
            v2_ok = is_v2_transition_allowed("job", current_state, to_state)
            try:
                legacy_ok = is_transition_allowed(current_state, to_state)
            except ValueError:
                legacy_ok = False  # V1 table knows only V1 states; V2 states raise
            if not (v2_ok or legacy_ok):
                raise RuntimeError(
                    f"Invalid state transition: {current_state} -> {to_state} for {job_id}"
                )
            current_version = expected_version
            if current_version is None:
                current_version = current_item.get("version", 0)
            current_epoch = current_item.get("execution_epoch", 0) or 0
            if expected_epoch is not None and expected_epoch != current_epoch:
                raise RuntimeError(
                    f"Epoch conflict for {job_id}: expected {expected_epoch}, "
                    f"found {current_epoch}"
                )
            new_version = (current_version or 0) + 1
            new_epoch = current_epoch + (1 if ownership_change else 0)

            current_active = current_item.get("active_migration_id")
            new_active = current_active
            if to_state == "MIGRATING":
                want = active_migration_id or attrs.get("active_migration_id")
                if current_active and want and current_active != want:
                    raise RuntimeError(
                        f"Job {job_id} already has active migration {current_active}"
                    )
                new_active = want or current_active
            if clear_active_migration or (
                to_state in ("RUNNING", "COMPLETED", "FAILED")
                and current_state == "MIGRATING"
            ):
                new_active = None
            elif active_migration_id is not None and to_state != "MIGRATING":
                new_active = active_migration_id

            names = {"#state": "state"}
            values = {
                ":state": to_state,
                ":version": new_version,
                ":epoch": new_epoch,
                ":active": new_active,
                ":last_updated": datetime.utcnow().isoformat(),
                ":expected": current_version,
            }
            expr_parts = [
                "#state = :state",
                "version = :version",
                "execution_epoch = :epoch",
                "active_migration_id = :active",
                "last_updated = :last_updated",
            ]
            for k, v in attrs.items():
                if k in ("active_migration_id", "execution_epoch", "version"):
                    continue
                ph_name = f"#{k}"
                ph_val = f":{k}"
                names[ph_name] = k
                values[ph_val] = v
                expr_parts.append(f"{ph_name} = {ph_val}")

            update_expr = "SET " + ", ".join(expr_parts)
            # Items predating V2 may lack execution_epoch: accept 0/None as match.
            if current_epoch in (0, None) and "execution_epoch" not in current_item:
                condition = "version = :expected AND attribute_not_exists(execution_epoch)"
            else:
                condition = "version = :expected AND execution_epoch = :expected_epoch"
                values[":expected_epoch"] = current_epoch

            try:
                self.table.update_item(
                    Key={"job_id": job_id},
                    UpdateExpression=update_expr,
                    ExpressionAttributeNames=names,
                    ExpressionAttributeValues=values,
                    ConditionExpression=condition,
                )
                get_metrics().inc("dynamodb_update_success_total")
                get_metrics().observe("dynamodb_update_duration_seconds", time.time() - start)
            except ClientError as e:
                if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                    get_metrics().inc("dynamodb_update_conflict_total")
                    raise RuntimeError(f"Optimistic lock failed for job_id {job_id}")
                get_metrics().inc("dynamodb_update_error_total")
                raise RuntimeError(f"Dynamo update failed: {e}")
            out = dict(current_item)
            out.update({"state": to_state, "version": new_version,
                        "execution_epoch": new_epoch,
                        "active_migration_id": new_active,
                        "last_updated": values[":last_updated"]})
            out.update({k: v for k, v in attrs.items()
                        if k not in ("active_migration_id", "execution_epoch", "version")})
            return out

    def list_by_state(self, state: str, use_gsi: bool = True):
        """
        List jobs by state. 
        
        Args:
            state: Job state to filter by
            use_gsi: If True, use GSI query (requires StateIndex). If False, falls back to scan.
        
        Returns:
            List of job items matching the state
        
        Note: For production, ensure DynamoDB table has a GSI named 'StateIndex' 
              with state as the partition key.
        """
        start = time.time()
        operation = "query" if use_gsi else "scan"
        
        try:
            items = []
            
            if use_gsi:
                # Use GSI query - 100x faster and cheaper than scan
                query_kwargs = {
                    "IndexName": "StateIndex",
                    "KeyConditionExpression": Key("state").eq(state)
                }
                while True:
                    resp = self.table.query(**query_kwargs)
                    items.extend(resp.get("Items", []))
                    if "LastEvaluatedKey" in resp:
                        query_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
                    else:
                        break
            else:
                # Fallback to scan (slow, expensive - only for dev/testing)
                scan_kwargs = {"FilterExpression": Attr("state").eq(state)}
                while True:
                    resp = self.table.scan(**scan_kwargs)
                    items.extend(resp.get("Items", []))
                    if "LastEvaluatedKey" in resp:
                        scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
                    else:
                        break
            
            get_metrics().inc(f"dynamodb_{operation}_success_total")
            get_metrics().observe(f"dynamodb_{operation}_duration_seconds", time.time() - start)
            get_metrics().set_gauge(f"dynamodb_{operation}_items_returned", len(items))
            return items
        except ClientError as e:
            get_metrics().inc(f"dynamodb_{operation}_error_total")
            # If GSI doesn't exist, provide helpful error
            if "ResourceNotFoundException" in str(e) and use_gsi:
                raise RuntimeError(
                    f"StateIndex GSI not found. Create it with: "
                    f"aws dynamodb update-table --table-name {self.table_name} "
                    f"--attribute-definitions AttributeName=state,AttributeType=S "
                    f"--global-secondary-index-updates '[{{\"Create\":{{\"IndexName\":\"StateIndex\","
                    f"\"KeySchema\":[{{\"AttributeName\":\"state\",\"KeyType\":\"HASH\"}}],"
                    f"\"Projection\":{{\"ProjectionType\":\"ALL\"}}}}}}]'"
                )
            raise RuntimeError(f"Dynamo list_by_state failed: {e}")

