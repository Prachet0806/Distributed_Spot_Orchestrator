from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional
import uuid
import logging

logger = logging.getLogger(__name__)


class ReadinessStatus(str, Enum):
    READY = "READY"
    PREPARABLE = "PREPARABLE"
    NOT_READY = "NOT_READY"
    UNKNOWN = "UNKNOWN"


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
class CapacityEvidence:
    status: CapacityStatus
    source: EvidenceSource
    observed_at: datetime
    confidence: float
    provisioning_success_rate: Optional[float] = None
    sample_count: int = 0
    snapshot_version: str = "1.0"


@dataclass
class IAMReadiness:
    secret_resolution_verified: bool = False
    kms_access_verified: bool = False
    instance_profile_ready: bool = False
    policy_attached: bool = False
    observed_at: datetime = field(default_factory=datetime.utcnow)

    def is_ready(self) -> bool:
        return all([self.secret_resolution_verified, self.kms_access_verified, self.instance_profile_ready, self.policy_attached])


@dataclass
class NetworkReadiness:
    vpc_configured: bool = False
    subnet_available: bool = False
    security_groups_ready: bool = False
    eni_attachable: bool = False
    observed_at: datetime = field(default_factory=datetime.utcnow)

    def is_ready(self) -> bool:
        return all([self.vpc_configured, self.subnet_available, self.security_groups_ready, self.eni_attachable])


@dataclass
class StorageReadiness:
    checkpoint_bucket_accessible: bool = False
    ebs_attachable: bool = False
    snapshot_creation_verified: bool = False
    observed_at: datetime = field(default_factory=datetime.utcnow)

    def is_ready(self) -> bool:
        return all([self.checkpoint_bucket_accessible, self.ebs_attachable, self.snapshot_creation_verified])


@dataclass
class ReadinessCheck:
    name: str
    status: ReadinessStatus
    reason: Optional[str] = None


@dataclass
class ReadinessAssessment:
    assessment_id: str
    pool_id: str
    status: ReadinessStatus
    checks: list[ReadinessCheck]
    capacity_evidence: CapacityEvidence
    iam_readiness: IAMReadiness
    network_readiness: NetworkReadiness
    storage_readiness: StorageReadiness
    artifact_available: bool = False
    runtime_ready: bool = False
    assessed_at: datetime = field(default_factory=datetime.utcnow)
    assessor_version: str = "v2-1"
    snapshot_version: str = "1.0"


class ReadinessEngine:
    def __init__(self, assessor_version: str = "v2-1"):
        self.assessor_version = assessor_version

    def assess(
        self,
        requirements: "WorkloadRequirements",
        pool: "CandidatePool",
        iam_ready: IAMReadiness = None,
        network_ready: NetworkReadiness = None,
        storage_ready: StorageReadiness = None,
        capacity_evidence: CapacityEvidence = None,
        artifact_available: bool = False,
    ) -> ReadinessAssessment:
        checks = []
        status = ReadinessStatus.READY

        if capacity_evidence is None:
            capacity_evidence = self._default_capacity_evidence(pool)
        if iam_ready is None:
            iam_ready = IAMReadiness()
        if network_ready is None:
            network_ready = NetworkReadiness()
        if storage_ready is None:
            storage_ready = StorageReadiness()

        artifact_check = self._check_artifact(pool, artifact_available)
        checks.append(artifact_check)
        if artifact_check.status != ReadinessStatus.READY:
            status = artifact_check.status

        iam_check = self._check_iam(iam_ready, requirements)
        checks.append(iam_check)
        if iam_check.status != ReadinessStatus.READY:
            status = iam_check.status

        network_check = self._check_network(network_ready)
        checks.append(network_check)
        if network_check.status != ReadinessStatus.READY:
            status = network_check.status

        storage_check = self._check_storage(storage_ready)
        checks.append(storage_check)
        if storage_check.status != ReadinessStatus.READY:
            status = storage_check.status

        capacity_check = self._check_capacity(capacity_evidence)
        checks.append(capacity_check)
        if capacity_check.status != ReadinessStatus.READY:
            status = capacity_check.status

        runtime_check = self._check_runtime(pool, requirements)
        checks.append(runtime_check)
        if runtime_check.status != ReadinessStatus.READY:
            status = runtime_check.status

        assessment = ReadinessAssessment(
            assessment_id=str(uuid.uuid4()),
            pool_id=pool.pool_id,
            status=status,
            checks=checks,
            capacity_evidence=capacity_evidence,
            iam_readiness=iam_ready,
            network_readiness=network_ready,
            storage_readiness=storage_ready,
            artifact_available=artifact_available,
            runtime_ready=runtime_check.status == ReadinessStatus.READY,
            assessed_at=datetime.utcnow(),
            assessor_version="v2-1",
            snapshot_version="1.0",
        )

        logger.info(f"Readiness assessment {assessment.assessment_id} for pool {pool.pool_id}: {status.value}")
        return assessment

    def _check_artifact(self, pool: "CandidatePool", artifact_available: bool) -> ReadinessCheck:
        if not artifact_available:
            return ReadinessCheck(
                name="artifact_available",
                status=ReadinessStatus.NOT_READY,
                reason="Runtime artifact not available on candidate",
            )
        return ReadinessCheck(name="artifact_available", status=ReadinessStatus.READY)

    def _check_iam(self, iam: IAMReadiness, req: "WorkloadRequirements") -> ReadinessCheck:
        if not iam.is_ready():
            missing = []
            if not iam.secret_resolution_verified:
                missing.append("secret_resolution")
            if not iam.kms_access_verified:
                missing.append("kms_access")
            if not iam.instance_profile_ready:
                missing.append("instance_profile")
            if not iam.policy_attached:
                missing.append("policy_attached")
            return ReadinessCheck(
                name="iam_ready",
                status=ReadinessStatus.NOT_READY,
                reason=f"IAM not ready: {missing}",
            )
        if req.required_secret_refs and not iam.secret_resolution_verified:
            return ReadinessCheck(
                name="iam_ready",
                status=ReadinessStatus.NOT_READY,
                reason="Secret resolution not verified for required refs",
            )
        return ReadinessCheck(name="iam_ready", status=ReadinessStatus.READY)

    def _check_network(self, network: NetworkReadiness) -> ReadinessCheck:
        if not network.is_ready():
            missing = []
            if not network.vpc_configured:
                missing.append("vpc")
            if not network.subnet_available:
                missing.append("subnet")
            if not network.security_groups_ready:
                missing.append("security_groups")
            if not network.eni_attachable:
                missing.append("eni")
            return ReadinessCheck(
                name="network_ready",
                status=ReadinessStatus.NOT_READY,
                reason=f"Network not ready: {missing}",
            )
        return ReadinessCheck(name="network_ready", status=ReadinessStatus.READY)

    def _check_storage(self, storage: StorageReadiness) -> ReadinessCheck:
        if not storage.is_ready():
            missing = []
            if not storage.checkpoint_bucket_accessible:
                missing.append("checkpoint_bucket")
            if not storage.ebs_attachable:
                missing.append("ebs")
            if not storage.snapshot_creation_verified:
                missing.append("snapshots")
            return ReadinessCheck(
                name="storage_ready",
                status=ReadinessStatus.NOT_READY,
                reason=f"Storage not ready: {missing}",
            )
        return ReadinessCheck(name="storage_ready", status=ReadinessStatus.READY)

    def _check_capacity(self, evidence: CapacityEvidence) -> ReadinessCheck:
        if evidence.status != CapacityStatus.AVAILABLE:
            return ReadinessCheck(
                name="capacity_allocatable",
                status=ReadinessStatus.NOT_READY,
                reason=f"Capacity status: {evidence.status.value} (confidence: {evidence.confidence:.2f})",
            )
        if evidence.confidence < 0.5:
            return ReadinessCheck(
                name="capacity_allocatable",
                status=ReadinessStatus.NOT_READY,
                reason=f"Capacity confidence too low: {evidence.confidence:.2f}",
            )
        return ReadinessCheck(name="capacity_allocatable", status=ReadinessStatus.READY)

    def _check_runtime(self, pool: "CandidatePool", req: "WorkloadRequirements") -> ReadinessCheck:
        if req.runtime_artifact_digest and pool.runtime_profile.artifact_digest != req.runtime_artifact_digest:
            return ReadinessCheck(
                name="runtime_ready",
                status=ReadinessStatus.NOT_READY,
                reason="Runtime artifact digest mismatch",
            )
        if req.gpu.required and (not pool.gpu_profile or not pool.gpu_profile.required):
            return ReadinessCheck(
                name="runtime_ready",
                status=ReadinessStatus.NOT_READY,
                reason="GPU required but pool has no GPU profile",
            )
        return ReadinessCheck(name="runtime_ready", status=ReadinessStatus.READY)

    def _default_capacity_evidence(self, pool: "CandidatePool") -> CapacityEvidence:
        return CapacityEvidence(
            status=CapacityStatus.AVAILABLE,
            source=EvidenceSource.HISTORICAL_PROVISIONING,
            observed_at=datetime.utcnow(),
            confidence=0.8,
            provisioning_success_rate=0.9,
            sample_count=10,
            snapshot_version="1.0",
        )

    def is_emergency_eligible(self, assessment: ReadinessAssessment) -> bool:
        return assessment.status == ReadinessStatus.READY