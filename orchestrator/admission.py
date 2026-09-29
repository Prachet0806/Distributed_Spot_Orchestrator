# orchestrator/admission.py — job admission (Phase A, baseline-locked).
"""Normal submission path (invariants 1-3): the user supplies a workload
contract, never PID/IP/AMI/Dynamo edits.

`spotctl run` builds a WorkloadContract, JobAdmissionManager validates it
and creates a PENDING registry row. Worker provisioning (PA-8),
telemetry PID/host discovery (PA-9) and RUNNING transition happen
downstream — never as user inputs.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional


CONTRACT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class WorkloadContract:
    """Immutable user intent. Frozen: downstream consumes, never mutates."""

    name: str
    script: str
    cpu: int = 1
    memory_mb: int = 1024
    gpu: bool = False
    storage_gb: int = 10
    regions: tuple = ()
    runtime_artifact_digest: str = ""
    python_version: str = ""
    criu_version: str = ""
    kernel_version: str = ""
    workload_type: str = "batch"
    max_recovery_time_s: int = 100
    schema_version: int = CONTRACT_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["regions"] = list(self.regions)
        d["resources"] = {
            "cpu": self.cpu, "memory_mb": self.memory_mb,
            "gpu": self.gpu, "storage_gb": self.storage_gb,
        }
        d["runtime"] = {
            "artifact_digest": self.runtime_artifact_digest,
            "python": self.python_version, "criu": self.criu_version,
            "kernel": self.kernel_version,
        }
        d["placement"] = {"regions": list(self.regions)}
        d["recovery"] = {"max_recovery_time_s": self.max_recovery_time_s}
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "WorkloadContract":
        res = dict(d.get("resources", {}))
        rt = dict(d.get("runtime", {}))
        pl = dict(d.get("placement", {}))
        rec = dict(d.get("recovery", {}))
        regions = pl.get("regions", d.get("regions", ()))
        return cls(
            name=d.get("name", d.get("job", "")),
            script=d.get("script", ""),
            cpu=int(res.get("cpu", d.get("cpu", 1))),
            memory_mb=int(res.get("memory_mb", d.get("memory_mb", 1024))),
            gpu=bool(res.get("gpu", d.get("gpu", False))),
            storage_gb=int(res.get("storage_gb", d.get("storage_gb", 10))),
            regions=tuple(regions or ()),
            runtime_artifact_digest=rt.get("artifact_digest",
                                           d.get("runtime_artifact_digest", "")),
            python_version=rt.get("python", d.get("python_version", "")),
            criu_version=rt.get("criu", d.get("criu_version", "")),
            kernel_version=rt.get("kernel", d.get("kernel_version", "")),
            workload_type=d.get("workload_type", "batch"),
            max_recovery_time_s=int(rec.get("max_recovery_time_s",
                                            d.get("max_recovery_time_s", 100))),
            schema_version=int(d.get("schema_version", CONTRACT_SCHEMA_VERSION)),
        )


@dataclass
class AdmissionResult:
    job_id: str
    verdict: str  # ADMITTED | REJECTED | DUPLICATE
    reason: str = ""
    contract: Optional[Dict[str, Any]] = None


class JobAdmissionManager:
    """Validate contracts, create PENDING rows. No PID/IP/AMI inputs."""

    def __init__(self, default_regions: Optional[List[str]] = None):
        self.default_regions = list(default_regions or [])

    def validate(self, contract: WorkloadContract) -> List[str]:
        errors = []
        if not contract.name:
            errors.append("name is required")
        if not contract.script:
            errors.append("script is required")
        if contract.cpu <= 0:
            errors.append("cpu must be > 0")
        if contract.memory_mb <= 0:
            errors.append("memory_mb must be > 0")
        if not contract.regions and not self.default_regions:
            errors.append("at least one region is required")
        return errors

    def admit(self, contract: WorkloadContract, registry: Any,
              job_id: Optional[str] = None) -> AdmissionResult:
        errors = self.validate(contract)
        if errors:
            return AdmissionResult(job_id or contract.name, "REJECTED",
                                   "; ".join(errors), contract.to_dict())
        jid = job_id or f"{contract.name}-{uuid.uuid4().hex[:8]}"
        regions = list(contract.regions) or list(self.default_regions)
        row = {
            "state": "PENDING",
            "region": regions[0] if regions else "",
            "public_ip": None,
            "pid": None,
            "workload_type": contract.workload_type,
            "workload_contract": contract.to_dict(),
            "admission": {"verdict": "ADMITTED", "reason": "contract-v1"},
        }
        # Idempotent resubmit: same job_id + same contract ⇒ DUPLICATE.
        try:
            existing = registry.get(jid)
            if isinstance(existing, dict) and existing.get("workload_contract") == row["workload_contract"]:
                return AdmissionResult(jid, "DUPLICATE", "identical contract resubmitted",
                                       contract.to_dict())
            return AdmissionResult(jid, "REJECTED",
                                   f"job {jid} exists with a different contract",
                                   contract.to_dict())
        except (KeyError, FileNotFoundError):
            pass
        except Exception as exc:
            return AdmissionResult(jid, "REJECTED", f"registry read failed: {exc}",
                                   contract.to_dict())
        try:
            if hasattr(registry, "create"):
                registry.create(jid, **row)
            else:  # JSON backend without create(): insert directly.
                import json
                with open(registry.path) as f:
                    data = json.load(f)
                if jid in data:
                    return AdmissionResult(jid, "REJECTED",
                                           f"job {jid} already exists",
                                           contract.to_dict())
                row["job_id"] = jid
                row["version"] = 0
                row["execution_epoch"] = 0
                data[jid] = row
                with open(registry.path, "w") as f:
                    json.dump(data, f, indent=2)
        except Exception as exc:
            return AdmissionResult(jid, "REJECTED", f"registry write failed: {exc}",
                                   contract.to_dict())
        return AdmissionResult(jid, "ADMITTED", "contract-v1", contract.to_dict())
