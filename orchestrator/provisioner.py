from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Any
import logging
import uuid

logger = logging.getLogger(__name__)


@dataclass
class ProvisionResult:
    instance_id: str
    public_ip: str
    public_dns: str
    status: str
    operation_id: str = ""
    provisioned_at: Any = None


class Provisioner:
    """Acquires/releases target compute.

    Real path: `provision_instance()` with `ClientToken=operation_id`
    (Protocols #20). Without an `ec2_client_factory`/AWS config the legacy
    in-memory stub answers (tests, `--engine v1` compat); the real path is
    used whenever region+AMI+SG+key are configured.
    """

    def __init__(
        self,
        ec2_client_factory: Any = None,
        region: Optional[str] = None,
        ami_id: Optional[str] = None,
        security_group_id: Optional[str] = None,
        key_name: Optional[str] = None,
        instance_type: Optional[str] = None,
        max_spot_price: Optional[str] = None,
    ):
        self.ec2_client_factory = ec2_client_factory
        self.region = region
        self.ami_id = ami_id
        self.security_group_id = security_group_id
        self.key_name = key_name
        self.instance_type = instance_type
        self.max_spot_price = max_spot_price
        self._provisioned_instances: dict[str, ProvisionResult] = {}
        self._operations: dict[str, str] = {}  # operation_id -> instance_id

    def _real_configured(self) -> bool:
        return bool(self.region and self.ami_id and self.security_group_id
                    and self.key_name and self.instance_type)

    def provision_with_operation(
        self,
        candidate_id: str,
        operation_id: Optional[str] = None,
        timeout: int = 300,
        tags: Optional[dict] = None,
    ) -> ProvisionResult:
        operation_id = operation_id or f"{int(datetime.utcnow().timestamp()*1000):013d}{uuid.uuid4().hex[:13]}"
        if operation_id in self._operations:
            # Idempotent replay: same operation returns the same instance.
            instance_id = self._operations[operation_id]
            for result in self._provisioned_instances.values():
                if result.instance_id == instance_id:
                    logger.info("Provision replay %s -> %s", operation_id[:8], instance_id)
                    return result
        if self._real_configured():
            from orchestrator.instance_manager import provision_instance
            instance_id, public_ip, public_dns = provision_instance(
                region=self.region,
                ami_id=self.ami_id,
                security_group_id=self.security_group_id,
                key_name=self.key_name,
                instance_type=self.instance_type,
                max_spot_price=self.max_spot_price,
                timeout=timeout,
                idempotency_token=operation_id,
                tags=tags,
            )
            result = ProvisionResult(
                instance_id=instance_id, public_ip=public_ip,
                public_dns=public_dns, status="running",
                operation_id=operation_id, provisioned_at=datetime.utcnow(),
            )
        else:
            result = self.provision(candidate_id)
            result.operation_id = operation_id
        self._operations[operation_id] = result.instance_id
        return result

    def get_operation_status(self, operation_id: str) -> dict:
        """Resolve UNKNOWN outcomes against actual state (never blind retry)."""
        instance_id = self._operations.get(operation_id)
        if instance_id is None:
            return {"operation_id": operation_id, "state": "UNKNOWN"}
        state = self.get_instance_state(instance_id)
        if state == "running":
            return {"operation_id": operation_id, "state": "SUCCEEDED",
                    "instance_id": instance_id}
        if state in ("terminated", "shutting-down", "stopping", "stopped"):
            return {"operation_id": operation_id, "state": "FAILED",
                    "instance_id": instance_id}
        return {"operation_id": operation_id, "state": "UNKNOWN",
                "instance_id": instance_id}

    def provision(self, candidate_id: str, **kwargs) -> ProvisionResult:
        logger.info(f"Provisioning candidate {candidate_id}")
        instance_id = f"i-{candidate_id[-12:]}"
        result = ProvisionResult(
            instance_id=instance_id,
            public_ip=f"10.0.{hash(candidate_id) % 255}.{hash(candidate_id) // 255 % 255}",
            public_dns=f"ec2-{instance_id}.compute-1.amazonaws.com",
            status="running",
        )
        self._provisioned_instances[candidate_id] = result
        return result

    def terminate(self, instance_id: str) -> bool:
        logger.info(f"Terminating instance {instance_id}")
        for cid, result in self._provisioned_instances.items():
            if result.instance_id == instance_id:
                del self._provisioned_instances[cid]
                return True
        return False

    def get_instance_state(self, instance_id: str) -> str:
        for result in self._provisioned_instances.values():
            if result.instance_id == instance_id:
                return result.status
        return "unknown"

    def is_ready(self, candidate_id: str) -> bool:
        return candidate_id in self._provisioned_instances