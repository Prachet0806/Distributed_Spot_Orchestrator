from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional, Callable, Any
import logging

logger = logging.getLogger(__name__)


class ReconciliationTrigger(str, Enum):
    EVENT_DRIVEN = "EVENT_DRIVEN"
    PERIODIC_SWEEP = "PERIODIC_SWEEP"
    MANUAL = "MANUAL"


class MismatchType(str, Enum):
    SOURCE_STILL_ALIVE = "SOURCE_STILL_ALIVE"
    TARGET_UNREACHABLE = "TARGET_UNREACHABLE"
    ORPHAN_RESOURCE = "ORPHAN_RESOURCE"
    EPOCH_MISMATCH = "EPOCH_MISMATCH"
    REGISTRY_INFRA_MISMATCH = "REGISTRY_INFRA_MISMATCH"
    MIGRATION_DANGLING = "MIGRATION_DANGLING"


class ReconciliationAction(str, Enum):
    REQUEST_REMEDIATION = "REQUEST_REMEDIATION"
    ESCALATE = "ESCALATE"
    AUTO_REMEDIATE = "AUTO_REMEDIATE"
    NO_ACTION = "NO_ACTION"


@dataclass
class ReconciliationFinding:
    finding_id: str
    job_id: str
    migration_id: Optional[str]
    mismatch_type: MismatchType
    severity: str
    details: dict
    detected_at: datetime
    trigger: ReconciliationTrigger
    suggested_action: ReconciliationAction
    confidence: float


@dataclass
class RemediationRequest:
    request_id: str
    finding_id: str
    job_id: str
    action_type: str
    parameters: dict
    priority: int
    requested_at: datetime
    authorized_by: Optional[str] = None


class ReconciliationManager:
    def __init__(
        self,
        registry: Any,
        infrastructure_monitor: Any,
        cleanup_executor: Any,
        remediation_callback: Optional[Callable] = None,
    ):
        self.registry = registry
        self.infrastructure_monitor = infrastructure_monitor
        self.cleanup_executor = cleanup_executor
        self.remediation_callback = remediation_callback

    def reconcile(self, trigger: ReconciliationTrigger = ReconciliationTrigger.PERIODIC_SWEEP) -> list[ReconciliationFinding]:
        findings = []

        if trigger == ReconciliationTrigger.PERIODIC_SWEEP:
            findings.extend(self._periodic_sweep())
        elif trigger == ReconciliationTrigger.EVENT_DRIVEN:
            findings.extend(self._event_driven_check())

        for finding in findings:
            self._process_finding(finding)

        return findings

    def _periodic_sweep(self) -> list[ReconciliationFinding]:
        findings = []

        active_jobs = self.registry.list_active_jobs()
        for job in active_jobs:
            if job.get("state") == "MIGRATING":
                findings.extend(self._check_active_migration(job))

            if job.get("state") == "RUNNING":
                findings.extend(self._check_running_job(job))

        findings.extend(self._check_orphan_resources())

        return findings

    def _event_driven_check(self) -> list[ReconciliationFinding]:
        return []

    def _check_active_migration(self, job: dict) -> list[ReconciliationFinding]:
        findings = []
        migration_id = job.get("active_migration_id")
        if not migration_id:
            return findings

        infra_state = self.infrastructure_monitor.get_instance_state(job.get("instance_id"))
        registry_state = job.get("state")

        if infra_state == "TERMINATED" and registry_state == "MIGRATING":
            findings.append(ReconciliationFinding(
                finding_id=f"REC-{datetime.utcnow().timestamp()}",
                job_id=job["job_id"],
                migration_id=migration_id,
                mismatch_type=MismatchType.REGISTRY_INFRA_MISMATCH,
                severity="HIGH",
                details={"infra_state": infra_state, "registry_state": registry_state},
                detected_at=datetime.utcnow(),
                trigger=ReconciliationTrigger.PERIODIC_SWEEP,
                suggested_action=ReconciliationAction.REQUEST_REMEDIATION,
                confidence=0.9,
            ))

        return findings

    def _check_running_job(self, job: dict) -> list[ReconciliationFinding]:
        findings = []
        instance_id = job.get("instance_id")
        if not instance_id:
            return findings

        infra_state = self.infrastructure_monitor.get_instance_state(instance_id)
        epoch = job.get("execution_epoch", 0)

        if infra_state == "TERMINATED":
            findings.append(ReconciliationFinding(
                finding_id=f"REC-{datetime.utcnow().timestamp()}",
                job_id=job["job_id"],
                migration_id=None,
                mismatch_type=MismatchType.SOURCE_STILL_ALIVE,
                severity="HIGH",
                details={"instance_id": instance_id, "infra_state": infra_state, "epoch": epoch},
                detected_at=datetime.utcnow(),
                trigger=ReconciliationTrigger.PERIODIC_SWEEP,
                suggested_action=ReconciliationAction.REQUEST_REMEDIATION,
                confidence=0.95,
            ))

        return findings

    def _check_orphan_resources(self) -> list[ReconciliationFinding]:
        return []

    def _process_finding(self, finding: ReconciliationFinding):
        logger.warning(f"Reconciliation finding: {finding.mismatch_type.value} for job {finding.job_id}")

        if finding.suggested_action == ReconciliationAction.REQUEST_REMEDIATION:
            self._request_remediation(finding)
        elif finding.suggested_action == ReconnectionAction.AUTO_REMEDIATE:
            self._auto_remediate(finding)
        elif finding.suggested_action == ReconciliationAction.ESCALATE:
            self._escalate(finding)

    def _request_remediation(self, finding: ReconciliationFinding):
        request = RemediationRequest(
            request_id=f"REM-{datetime.utcnow().timestamp()}",
            finding_id=finding.finding_id,
            job_id=finding.job_id,
            action_type=finding.mismatch_type.value,
            parameters=finding.details,
            priority=1 if finding.severity == "HIGH" else 2,
            requested_at=datetime.utcnow(),
        )
        if self.remediation_callback:
            self.remediation_callback(request)

    def _auto_remediate(self, finding: ReconciliationFinding):
        if finding.mismatch_type == MismatchType.ORPHAN_RESOURCE:
            self.cleanup_executor.cleanup_orphan(finding.details.get("resource_id"))

    def _escalate(self, finding: ReconciliationFinding):
        logger.error(f"ESCALATION: {finding.mismatch_type.value} for job {finding.job_id} - manual intervention required")


class ReconnectionAction(str, Enum):
    REQUEST_REMEDIATION = "REQUEST_REMEDIATION"
    ESCALATE = "ESCALATE"
    AUTO_REMEDIATE = "AUTO_REMEDIATE"
    NO_ACTION = "NO_ACTION"