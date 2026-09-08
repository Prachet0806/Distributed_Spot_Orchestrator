from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional
import uuid
import logging

logger = logging.getLogger(__name__)


class CompatibilityStatus(str, Enum):
    COMPATIBLE = "COMPATIBLE"
    INCOMPATIBLE = "INCOMPATIBLE"
    UNKNOWN = "UNKNOWN"


@dataclass
class CompatibilityDimensionResult:
    dimension: str
    status: CompatibilityStatus
    reason: Optional[str] = None


@dataclass
class CompatibilityAssessment:
    assessment_id: str
    pool_id: str
    workload_requirements_version: str
    status: CompatibilityStatus
    dimensions: list[CompatibilityDimensionResult]
    assessed_at: datetime
    assessor_version: str = "v2-1"


class CompatibilityEngine:
    def __init__(self, assessor_version: str = "v2-1"):
        self.assessor_version = assessor_version

    def assess(
        self,
        requirements: "WorkloadRequirements",
        pool: "CandidatePool",
    ) -> CompatibilityAssessment:
        dimensions = []
        status = CompatibilityStatus.COMPATIBLE

        dim_status = self._check_architecture(requirements, pool)
        dimensions.append(dim_status)
        if dim_status.status != CompatibilityStatus.COMPATIBLE:
            status = dim_status.status

        dim_status = self._check_cpu(requirements, pool)
        dimensions.append(dim_status)
        if dim_status.status != CompatibilityStatus.COMPATIBLE:
            status = dim_status.status

        dim_status = self._check_memory(requirements, pool)
        dimensions.append(dim_status)
        if dim_status.status != CompatibilityStatus.COMPATIBLE:
            status = dim_status.status

        dim_status = self._check_gpu(requirements, pool)
        dimensions.append(dim_status)
        if dim_status.status != CompatibilityStatus.COMPATIBLE:
            status = dim_status.status

        dim_status = self._check_storage(requirements, pool)
        dimensions.append(dim_status)
        if dim_status.status != CompatibilityStatus.COMPATIBLE:
            status = dim_status.status

        dim_status = self._check_capabilities(requirements, pool)
        dimensions.append(dim_status)
        if dim_status.status != CompatibilityStatus.COMPATIBLE:
            status = dim_status.status

        dim_status = self._check_runtime(requirements, pool)
        dimensions.append(dim_status)
        if dim_status.status != CompatibilityStatus.COMPATIBLE:
            status = dim_status.status

        dim_status = self._check_region_az(requirements, pool)
        dimensions.append(dim_status)
        if dim_status.status != CompatibilityStatus.COMPATIBLE:
            status = dim_status.status

        dim_status = self._check_reconnectability(requirements, pool)
        dimensions.append(dim_status)
        if dim_status.status != CompatibilityStatus.COMPATIBLE:
            status = dim_status.status

        assessment = CompatibilityAssessment(
            assessment_id=str(uuid.uuid4()),
            pool_id=pool.pool_id,
            workload_requirements_version="1.0",
            status=status,
            dimensions=dimensions,
            assessed_at=datetime.utcnow(),
            assessor_version="v2-1",
        )

        logger.info(f"Compatibility assessment {assessment.assessment_id} for pool {pool.pool_id}: {status.value}")
        return assessment

    def _check_architecture(self, req: "WorkloadRequirements", pool: "CandidatePool") -> CompatibilityDimensionResult:
        if req.cpu_architecture != pool.architecture:
            return CompatibilityDimensionResult(
                dimension="architecture",
                status=CompatibilityStatus.INCOMPATIBLE,
                reason=f"Architecture mismatch: {req.cpu_architecture} != {pool.architecture}",
            )
        return CompatibilityDimensionResult(dimension="architecture", status=CompatibilityStatus.COMPATIBLE)

    def _check_cpu(self, req: "WorkloadRequirements", pool: "CandidatePool") -> CompatibilityDimensionResult:
        if pool.instance_type:
            pool_cpu = self._extract_cpu(pool.instance_type)
            if pool_cpu < req.min_cpu:
                return CompatibilityDimensionResult(
                    dimension="cpu",
                    status=CompatibilityStatus.INCOMPATIBLE,
                    reason=f"Insufficient CPU: {pool_cpu} < {req.min_cpu}",
                )
        return CompatibilityDimensionResult(dimension="cpu", status=CompatibilityStatus.COMPATIBLE)

    def _check_memory(self, req: "WorkloadRequirements", pool: "CandidatePool") -> CompatibilityDimensionResult:
        if pool.instance_type:
            pool_mem = self._extract_memory_mb(pool.instance_type)
            if pool_mem < req.min_memory_mb:
                return CompatibilityDimensionResult(
                    dimension="memory",
                    status=CompatibilityStatus.INCOMPATIBLE,
                    reason=f"Insufficient memory: {pool_mem} < {req.min_memory_mb}",
                )
        return CompatibilityDimensionResult(dimension="memory", status=CompatibilityStatus.COMPATIBLE)

    def _check_gpu(self, req: "WorkloadRequirements", pool: "CandidatePool") -> CompatibilityDimensionResult:
        if req.gpu.required:
            if not pool.gpu_profile or not pool.gpu_profile.required:
                return CompatibilityDimensionResult(
                    dimension="gpu",
                    status=CompatibilityStatus.INCOMPATIBLE,
                    reason="GPU required but pool has no GPU profile",
                )
            if not req.gpu.matches(pool.gpu_profile):
                return CompatibilityDimensionResult(
                    dimension="gpu",
                    status=CompatibilityStatus.INCOMPATIBLE,
                    reason="GPU requirements not satisfied by pool profile",
                )
        return CompatibilityDimensionResult(dimension="gpu", status=CompatibilityStatus.COMPATIBLE)

    def _check_storage(self, req: "WorkloadRequirements", pool: "CandidatePool") -> CompatibilityDimensionResult:
        if pool.capacity_profile.max_instances > 0 and req.min_storage_gb > pool.capacity_profile.max_instances * 10:
            return CompatibilityDimensionResult(
                dimension="storage",
                status=CompatibilityStatus.INCOMPATIBLE,
                reason=f"Insufficient storage capacity for {req.min_storage_gb}GB requirement",
            )
        return CompatibilityDimensionResult(dimension="storage", status=CompatibilityStatus.COMPATIBLE)

    def _check_capabilities(self, req: "WorkloadRequirements", pool: "CandidatePool") -> CompatibilityDimensionResult:
        missing = set(req.required_capabilities) - set(pool.runtime_profile.required_libraries)
        if missing:
            return CompatibilityDimensionResult(
                dimension="capabilities",
                status=CompatibilityStatus.INCOMPATIBLE,
                reason=f"Missing capabilities: {missing}",
            )
        return CompatibilityDimensionResult(dimension="capabilities", status=CompatibilityStatus.COMPATIBLE)

    def _check_runtime(self, req: "WorkloadRequirements", pool: "CandidatePool") -> CompatibilityDimensionResult:
        if req.runtime_artifact_digest and pool.runtime_profile.artifact_digest != req.runtime_artifact_digest:
            return CompatibilityDimensionResult(
                dimension="runtime",
                status=CompatibilityStatus.INCOMPATIBLE,
                reason="Runtime artifact digest mismatch",
            )
        return CompatibilityDimensionResult(dimension="runtime", status=CompatibilityStatus.COMPATIBLE)

    def _check_region_az(self, req: "WorkloadRequirements", pool: "CandidatePool") -> CompatibilityDimensionResult:
        if req.placement.allowed_regions and pool.region not in req.placement.allowed_regions:
            return CompatibilityDimensionResult(
                dimension="region",
                status=CompatibilityStatus.INCOMPATIBLE,
                reason=f"Region {pool.region} not in allowed regions",
            )
        if req.placement.allowed_azs and pool.availability_zone not in req.placement.allowed_azs:
            return CompatibilityDimensionResult(
                dimension="availability_zone",
                status=CompatibilityStatus.INCOMPATIBLE,
                reason=f"AZ {pool.availability_zone} not in allowed AZs",
            )
        return CompatibilityDimensionResult(dimension="region_az", status=CompatibilityStatus.COMPATIBLE)

    def _check_reconnectability(self, req: "WorkloadRequirements", pool: "CandidatePool") -> CompatibilityDimensionResult:
        if not req.reconnectable:
            return CompatibilityDimensionResult(
                dimension="reconnectability",
                status=CompatibilityStatus.INCOMPATIBLE,
                reason="Workload requires non-reconnectable migration which is not supported in V2",
            )
        return CompatibilityDimensionResult(dimension="reconnectability", status=CompatibilityStatus.COMPATIBLE)

    def _extract_cpu(self, instance_type: str) -> int:
        parts = instance_type.split(".")
        if len(parts) >= 2:
            try:
                return int(parts[1])
            except ValueError:
                pass
        return 1

    def _extract_memory_mb(self, instance_type: str) -> int:
        type_prefix = instance_type.split(".")[0]
        memory_map = {
            "t3": {"nano": 512, "micro": 1024, "small": 2048, "medium": 4096, "large": 8192, "xlarge": 16384, "2xlarge": 32768},
            "t2": {"nano": 512, "micro": 1024, "small": 2048, "medium": 4096, "large": 8192, "xlarge": 16384, "2xlarge": 32768},
            "m5": {"large": 8192, "xlarge": 16384, "2xlarge": 32768, "4xlarge": 65536, "12xlarge": 196608, "24xlarge": 393216},
            "c5": {"large": 4096, "xlarge": 8192, "2xlarge": 16384, "4xlarge": 32768, "9xlarge": 73728, "18xlarge": 147456},
            "r5": {"large": 16384, "xlarge": 32768, "2xlarge": 65536, "4xlarge": 131072, "12xlarge": 393216, "24xlarge": 786432},
        }
        family = type_prefix
        size = instance_type.split(".")[-1] if "." in instance_type else ""
        if family in memory_map and size in memory_map[family]:
            return memory_map[family][size]
        return 1024