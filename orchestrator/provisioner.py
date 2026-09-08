from dataclasses import dataclass
from typing import Optional, Any
import logging

logger = logging.getLogger(__name__)


@dataclass
class ProvisionResult:
    instance_id: str
    public_ip: str
    public_dns: str
    status: str


class Provisioner:
    def __init__(self, ec2_client_factory: Any = None):
        self.ec2_client_factory = ec2_client_factory
        self._provisioned_instances: dict[str, ProvisionResult] = {}

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