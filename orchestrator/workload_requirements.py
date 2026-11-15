from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from orchestrator.candidate_pool import PoolGPUProfile


class GPURequirement:
    def __init__(
        self,
        required: bool = False,
        vendor: str | None = None,
        model: str | None = None,
        memory_gb: int | None = None,
        compute_capability: str | None = None,
    ):
        self.required = required
        self.vendor = vendor
        self.model = model
        self.memory_gb = memory_gb
        self.compute_capability = compute_capability

    def matches(self, profile: PoolGPUProfile) -> bool:
        if not self.required:
            return True
        if not profile or not profile.required:
            return False
        if self.vendor and profile.vendor != self.vendor:
            return False
        if self.model and profile.model != self.model:
            return False
        if self.memory_gb and (profile.memory_gb or 0) < self.memory_gb:
            return False
        if (
            self.compute_capability
            and profile.compute_capability != self.compute_capability
        ):
            return False
        return True


@dataclass
class PlacementConstraints:
    allowed_regions: list[str] = field(default_factory=list)
    allowed_azs: list[str] = field(default_factory=list)
    affinity: list[str] = field(default_factory=list)
    anti_affinity: list[str] = field(default_factory=list)


@dataclass
class WorkloadRequirements:
    cpu_architecture: str
    min_cpu: int
    min_memory_mb: int
    gpu: GPURequirement = field(default_factory=GPURequirement)
    min_storage_gb: int = 10
    required_capabilities: list[str] = field(default_factory=list)
    runtime_artifact_digest: str | None = None
    required_secret_refs: list[str] = field(default_factory=list)
    placement: PlacementConstraints = field(default_factory=PlacementConstraints)
    reconnectable: bool = True

    def __post_init__(self):
        if isinstance(self.gpu, dict):
            self.gpu = GPURequirement(**self.gpu)
        if isinstance(self.placement, dict):
            self.placement = PlacementConstraints(**self.placement)
