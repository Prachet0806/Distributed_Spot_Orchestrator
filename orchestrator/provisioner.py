"""Pool-authoritative AWS provisioning and explicit V2 emulation."""
from dataclasses import dataclass
from datetime import datetime
import uuid


@dataclass
class ProvisionResult:
    instance_id: str
    public_ip: str
    public_dns: str
    status: str
    operation_id: str = ""
    provisioned_at: object = None
    region: str = ""


class Provisioner:
    """AWS executor. Incomplete pool definitions fail without emulation."""

    def __init__(self, key_name=None, max_spot_price=None, *, operation_store=None, regions=(), ec2_factory=None):
        self.key_name = key_name
        self.max_spot_price = max_spot_price
        self._operations = {}
        self._regions = {}
        self.operation_store = operation_store
        self.regions = tuple(regions)
        self.ec2_factory = ec2_factory
        if operation_store is not None:
            for doc in operation_store._docs.all():
                result = doc.get("result") or {}
                if isinstance(result, dict) and result.get("instance_id") and result.get("region"):
                    self._regions[result["instance_id"]] = result["region"]

    def provision_from_pool(self, definition, operation_id=None, timeout=300, tags=None):
        from storage.pool_registry import validate_pool_definition
        from orchestrator.instance_manager import provision_instance
        errors = validate_pool_definition(definition)
        if definition.get("provider", "aws") != "aws":
            errors.append("AWS provisioner requires provider=aws")
        if not self.key_name:
            errors.append("SSH key name is required")
        if errors:
            raise ValueError("Invalid target pool: " + "; ".join(errors))
        operation_id = operation_id or uuid.uuid4().hex
        if operation_id in self._operations:
            return self._operations[operation_id]
        if self.operation_store is not None:
            journal = self.operation_store.begin(f"provider:{operation_id}",
                {"external_operation_id": operation_id, "definition": definition, "tags": tags or {}})
            if journal["state"] == "SUCCEEDED":
                data = dict(journal["result"])
                result = ProvisionResult(**data)
                self._operations[operation_id] = result
                self._regions[result.instance_id] = result.region
                return result
        tags = {**(tags or {}), "operation_id": operation_id, "managed_by": "spot-arbitrage-v2"}
        from orchestrator.worker_bootstrap import control_userdata
        user_data = control_userdata(int(tags.get("execution_epoch", 0)))
        iid, ip, dns = provision_instance(
            region=definition["region"], ami_id=definition["ami_id"],
            security_group_id=definition["security_group_id"],
            key_name=self.key_name, instance_type=definition["instance_type"],
            max_spot_price=self.max_spot_price, timeout=timeout,
            idempotency_token=operation_id, tags=tags, iam_profile=definition.get("iam_profile"),
            subnet_id=definition.get("subnet_id"), user_data=user_data)
        result = ProvisionResult(iid, ip, dns, "running", operation_id, datetime.utcnow(), definition["region"])
        if self.operation_store is not None:
            self.operation_store.finish(journal["operation_id"], "SUCCEEDED", result=result)
        self._operations[operation_id] = result
        self._regions[iid] = definition["region"]
        return result

    def get_operation_status(self, operation_id):
        result = self._operations.get(operation_id)
        if result is None:
            for region in self.regions:
                ec2 = self._client(region)
                response = ec2.describe_instances(Filters=[
                    {"Name": "tag:operation_id", "Values": [operation_id]},
                    {"Name": "tag:managed_by", "Values": ["spot-arbitrage-v2"]}])
                instances = [instance for reservation in response.get("Reservations", [])
                             for instance in reservation.get("Instances", [])]
                if len(instances) > 1:
                    return {"operation_id": operation_id, "state": "UNKNOWN", "reason": "multiple instances"}
                if instances:
                    instance = instances[0]
                    self._regions[instance["InstanceId"]] = region
                    if instance["State"]["Name"] == "running" and instance.get("PublicIpAddress"):
                        result = ProvisionResult(instance["InstanceId"], instance["PublicIpAddress"],
                            instance.get("PublicDnsName", ""), "running", operation_id, None, region)
                        self._operations[operation_id] = result
                        break
            if result is None:
                return {"operation_id": operation_id, "state": "UNKNOWN"}
        state = self.get_instance_state(result.instance_id)
        return {"operation_id": operation_id,
                "state": "SUCCEEDED" if state == "running" else
                "FAILED" if state in {"terminated", "shutting-down", "stopped", "stopping"}
                else "UNKNOWN", "instance_id": result.instance_id,
                "result": result}

    def _client(self, region):
        import boto3
        return self.ec2_factory(region) if self.ec2_factory else boto3.client("ec2", region_name=region)

    def get_instance_state(self, instance_id):
        region = self._regions.get(instance_id)
        if region is None:
            return "unknown"
        response = self._client(region).describe_instances(
            InstanceIds=[instance_id])
        return response["Reservations"][0]["Instances"][0]["State"]["Name"]

    def terminate(self, instance_id):
        import boto3
        region = self._regions.get(instance_id)
        if region is None:
            raise ValueError(f"Unknown instance ownership: {instance_id}")
        self._client(region).terminate_instances(InstanceIds=[instance_id])
        return True


class EmulatedProvisioner:
    """Explicit in-process executor for emulated tests; never constructed on AWS paths."""

    def __init__(self):
        self._operations = {}
        self._instances = {}

    def provision_with_operation(self, pool_id, operation_id=None, timeout=300, tags=None):
        operation_id = operation_id or uuid.uuid4().hex
        if operation_id in self._operations:
            return self._operations[operation_id]
        result = self._launch(pool_id, operation_id)
        self._operations[operation_id] = result
        self._instances[result.instance_id] = result
        return result

    def _launch(self, pool_id, operation_id):
        return ProvisionResult("i-" + uuid.uuid4().hex[:17], "127.0.0.1",
                               "localhost", "running", operation_id, datetime.utcnow())

    def provision_from_pool(self, definition, operation_id=None, timeout=300, tags=None):
        return self.provision_with_operation(definition["pool_id"], operation_id, timeout, tags)

    def get_operation_status(self, operation_id):
        result = self._operations.get(operation_id)
        if result is None:
            return {"operation_id": operation_id, "state": "UNKNOWN"}
        return {"operation_id": operation_id,
                "state": "SUCCEEDED" if result.instance_id in self._instances else "FAILED",
                "instance_id": result.instance_id, "result": result}

    def terminate(self, instance_id):
        return self._instances.pop(instance_id, None) is not None

    def get_instance_state(self, instance_id):
        return "running" if instance_id in self._instances else "terminated"
