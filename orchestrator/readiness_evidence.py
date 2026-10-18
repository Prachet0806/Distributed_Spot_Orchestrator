"""Candidate-specific evidence. Absence/expiry never becomes a green gate."""
from dataclasses import fields
from datetime import datetime, timezone
from orchestrator.candidate_readiness import (
    IAMReadiness, NetworkReadiness, StorageReadiness, CapacityEvidence,
    CapacityStatus, EvidenceSource,
)


def readiness_arguments(evidence, now=None):
    now = now or datetime.now(timezone.utc)
    evidence = evidence if isinstance(evidence, dict) else {}
    try:
        observed = datetime.fromisoformat(evidence["observed_at"].replace("Z", "+00:00"))
        observed = observed.replace(tzinfo=timezone.utc) if observed.tzinfo is None else observed
        fresh = -5 <= (now - observed).total_seconds() <= 60
    except (KeyError, ValueError, TypeError):
        observed, fresh = now, False
    def domain(cls, name):
        raw = evidence.get(name, {}) if fresh else {}
        return cls(**{field.name: raw.get(field.name) is True for field in fields(cls)})
    capacity = evidence.get("capacity", {}) if fresh else {}
    confidence = capacity.get("confidence", 0)
    if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        confidence = 0
    return {
        "iam_ready": domain(IAMReadiness, "iam"),
        "network_ready": domain(NetworkReadiness, "network"),
        "storage_ready": domain(StorageReadiness, "storage"),
        "capacity_evidence": CapacityEvidence(
            status=CapacityStatus.AVAILABLE if capacity.get("available") is True else CapacityStatus.UNKNOWN,
            source=EvidenceSource.PROVIDER_SIGNAL, observed_at=observed.replace(tzinfo=None),
            confidence=confidence, provisioning_success_rate=confidence,
            sample_count=int(capacity.get("sample_count", 0))),
        "artifact_available": fresh and evidence.get("artifact_available") is True,
    }
