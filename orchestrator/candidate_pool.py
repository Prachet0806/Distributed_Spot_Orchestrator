from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List
from enum import Enum
import uuid


class EvidenceSource(str, Enum):
    PROVIDER_SIGNAL = "PROVIDER_SIGNAL"
    HISTORICAL_PROVISIONING = "HISTORICAL_PROVISIONING"
    RECENT_PROVISIONING = "RECENT_PROVISIONING"
    ACTIVE_PROVISIONING_RECORD = "ACTIVE_PROVISIONING_RECORD"


class CapacityStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    LIMITED = "LIMITED"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


@dataclass
class PoolGPUProfile:
    required: bool = False
    vendor: Optional[str] = None
    model: Optional[str] = None
    memory_gb: Optional[int] = None
    compute_capability: Optional[str] = None


@dataclass
class PoolRuntimeProfile:
    artifact_digest: str
    architecture: str
    kernel_version: Optional[str] = None
    criu_version: Optional[str] = None
    required_libraries: List[str] = field(default_factory=list)


@dataclass
class PoolCapacityProfile:
    max_instances: int = 10
    reserved_instances: int = 0
    provisioning_timeout_seconds: int = 300


@dataclass
class PoolPlacementConstraints:
    allowed_regions: List[str] = field(default_factory=list)
    allowed_azs: List[str] = field(default_factory=list)
    affinity: List[str] = field(default_factory=list)
    anti_affinity: List[str] = field(default_factory=list)


@dataclass
class CandidatePool:
    pool_id: str
    provider: str
    account_id: str
    region: str
    availability_zone: str
    instance_type: str
    architecture: str
    gpu_profile: Optional[PoolGPUProfile] = None
    runtime_profile: PoolRuntimeProfile = field(default_factory=PoolRuntimeProfile)
    capacity_profile: PoolCapacityProfile = field(default_factory=PoolCapacityProfile)
    placement_constraints: PoolPlacementConstraints = field(default_factory=PoolPlacementConstraints)
    created_at: datetime = field(default_factory=datetime.utcnow)
    version: int = 1

    def to_dict(self) -> dict:
        return {
            "pool_id": self.pool_id,
            "provider": self.provider,
            "account_id": self.account_id,
            "region": self.region,
            "availability_zone": self.availability_zone,
            "instance_type": self.instance_type,
            "architecture": self.architecture,
            "gpu_profile": self.gpu_profile.__dict__ if self.gpu_profile else None,
            "runtime_profile": self.runtime_profile.__dict__,
            "capacity_profile": self.capacity_profile.__dict__,
            "placement_constraints": self.placement_constraints.__dict__,
            "created_at": self.created_at.isoformat(),
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CandidatePool":
        gpu = data.get("gpu_profile")
        if gpu:
            gpu = PoolGPUProfile(**gpu)
        runtime = PoolRuntimeProfile(**data.get("runtime_profile", {}))
        capacity = PoolCapacityProfile(**data.get("capacity_profile", {}))
        constraints = PoolPlacementConstraints(**data.get("placement_constraints", {}))
        return cls(
            pool_id=data["pool_id"],
            provider=data["provider"],
            account_id=data["account_id"],
            region=data["region"],
            availability_zone=data["availability_zone"],
            instance_type=data["instance_type"],
            architecture=data["architecture"],
            gpu_profile=gpu,
            runtime_profile=runtime,
            capacity_profile=capacity,
            placement_constraints=constraints,
            created_at=datetime.fromisoformat(data["created_at"]),
            version=data.get("version", 1),
        )


class CandidatePoolRegistry:
    def __init__(self, table_name: str = None, dynamodb_resource: any = None):
        self.table_name = table_name or "spot_arbitrage_candidate_pools"
        self.dynamodb = dynamodb_resource
        self._local_cache: dict[str, CandidatePool] = {}

    def create(self, pool: CandidatePool) -> CandidatePool:
        if self.dynamodb:
            table = self.dynamodb.Table(self.table_name)
            table.put_item(Item=pool.to_dict())
        else:
            self._local_cache[pool.pool_id] = pool
        return pool

    def get(self, pool_id: str) -> Optional[CandidatePool]:
        if self.dynamodb:
            table = self.dynamodb.Table(self.table_name)
            resp = table.get_item(Key={"pool_id": pool_id})
            if "Item" in resp:
                return CandidatePool.from_dict(resp["Item"])
        else:
            return self._local_cache.get(pool_id)
        return None

    def list_all(self) -> List[CandidatePool]:
        if self.dynamodb:
            table = self.dynamodb.Table(self.table_name)
            items = []
            resp = table.scan()
            items.extend(resp.get("Items", []))
            while "LastEvaluatedKey" in resp:
                resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
                items.extend(resp.get("Items", []))
            return [CandidatePool.from_dict(item) for item in items]
        else:
            return list(self._local_cache.values())

    def update_version(self, pool_id: str, expected_version: int, new_version: int) -> bool:
        if self.dynamodb:
            table = self.dynamodb.Table(self.table_name)
            try:
                table.update_item(
                    Key={"pool_id": pool_id},
                    UpdateExpression="SET version = :nv",
                    ConditionExpression="version = :ev",
                    ExpressionAttributeValues={":nv": new_version, ":ev": expected_version},
                )
                return True
            except Exception:
                return False
        else:
            pool = self._local_cache.get(pool_id)
            if pool and pool.version == expected_version:
                pool.version = new_version
                return True
            return False