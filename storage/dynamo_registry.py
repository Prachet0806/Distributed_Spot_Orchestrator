import boto3
from botocore.exceptions import ClientError
from boto3.dynamodb.conditions import Attr
from threading import Lock
from datetime import datetime


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
        try:
            resp = self.table.get_item(Key={"job_id": job_id})
            if "Item" not in resp:
                raise KeyError(f"job_id {job_id} not found")
            return resp["Item"]
        except ClientError as e:
            raise RuntimeError(f"Dynamo get failed: {e}")

    def create(self, job_id: str, **attrs):
        item = {
            "job_id": job_id,
            "version": 0,
            "last_updated": datetime.utcnow().isoformat(),
            **attrs,
        }
        try:
            self.table.put_item(Item=item, ConditionExpression="attribute_not_exists(job_id)")
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise KeyError(f"job_id {job_id} already exists")
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
        with self.lock:
            current_version = expected_version
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
            except ClientError as e:
                if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                    raise RuntimeError(f"Optimistic lock failed for job_id {job_id}")
                raise RuntimeError(f"Dynamo update failed: {e}")

    def list_by_state(self, state: str):
        """
        List jobs by state. Uses Scan with a filter (add a GSI on state for scale if needed).
        """
        try:
            items = []
            scan_kwargs = {"FilterExpression": Attr("state").eq(state)}
            while True:
                resp = self.table.scan(**scan_kwargs)
                items.extend(resp.get("Items", []))
                if "LastEvaluatedKey" in resp:
                    scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
                else:
                    break
            return items
        except ClientError as e:
            raise RuntimeError(f"Dynamo list_by_state failed: {e}")

