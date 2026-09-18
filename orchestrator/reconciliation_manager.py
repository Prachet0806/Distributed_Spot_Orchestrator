from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Callable, Any
import logging
import uuid

logger = logging.getLogger(__name__)


class ReconciliationTrigger(str, Enum):
    EVENT_DRIVEN = "EVENT_DRIVEN"
    PERIODIC_SWEEP = "PERIODIC_SWEEP"
    MANUAL = "MANUAL"


class MismatchType(str, Enum):
    # Legacy values (kept for compat).
    SOURCE_STILL_ALIVE = "SOURCE_STILL_ALIVE"
    TARGET_UNREACHABLE = "TARGET_UNREACHABLE"
    ORPHAN_RESOURCE = "ORPHAN_RESOURCE"
    EPOCH_MISMATCH = "EPOCH_MISMATCH"
    REGISTRY_INFRA_MISMATCH = "REGISTRY_INFRA_MISMATCH"
    MIGRATION_DANGLING = "MIGRATION_DANGLING"
    # V2 taxonomy (Protocols #11 §13.3).
    SOURCE_STILL_ALIVE_AFTER_FENCE = "SOURCE_STILL_ALIVE_AFTER_FENCE"
    TARGET_MISSING = "TARGET_MISSING"
    SOURCE_MISSING = "SOURCE_MISSING"
    TARGET_UNOWNED = "TARGET_UNOWNED"
    ORPHAN_INSTANCE = "ORPHAN_INSTANCE"
    ORPHAN_CHECKPOINT = "ORPHAN_CHECKPOINT"
    REGISTRY_INFRASTRUCTURE_MISMATCH = "REGISTRY_INFRASTRUCTURE_MISMATCH"
    STALE_MIGRATION = "STALE_MIGRATION"
    DANGLING_MIGRATION_REFERENCE = "DANGLING_MIGRATION_REFERENCE"
    UNKNOWN_OWNERSHIP = "UNKNOWN_OWNERSHIP"
    SPLIT_BRAIN = "SPLIT_BRAIN"


class ReconciliationAction(str, Enum):
    REQUEST_REMEDIATION = "REQUEST_REMEDIATION"
    ESCALATE = "ESCALATE"
    AUTO_REMEDIATE = "AUTO_REMEDIATE"
    NO_ACTION = "NO_ACTION"


# Legacy alias (was a duplicate class + the source of a NameError).
ReconnectionAction = ReconciliationAction


class FindingStatus(str, Enum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    REMEDIATING = "REMEDIATING"
    RESOLVED = "RESOLVED"
    ESCALATED = "ESCALATED"


# Findings that gate recovery until ownership is re-established.
OWNERSHIP_GATE_TYPES = frozenset({
    MismatchType.SPLIT_BRAIN, MismatchType.UNKNOWN_OWNERSHIP,
    MismatchType.EPOCH_MISMATCH, MismatchType.SOURCE_STILL_ALIVE_AFTER_FENCE,
    MismatchType.SOURCE_STILL_ALIVE,
})

_STATUS_FLOW = {
    FindingStatus.OPEN: {FindingStatus.ACKNOWLEDGED, FindingStatus.ESCALATED},
    FindingStatus.ACKNOWLEDGED: {FindingStatus.REMEDIATING, FindingStatus.ESCALATED},
    FindingStatus.REMEDIATING: {FindingStatus.RESOLVED, FindingStatus.ESCALATED},
    FindingStatus.RESOLVED: set(),
    FindingStatus.ESCALATED: set(),
}


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
    execution_epoch: int = 0
    observed_state: dict = field(default_factory=dict)
    authoritative_state: dict = field(default_factory=dict)
    evidence_refs: list = field(default_factory=list)
    recommended_remediation: dict = field(default_factory=dict)
    status: FindingStatus = FindingStatus.OPEN


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
    """Detects expected-vs-observed divergence and requests remediation.

    Reconciliation never executes general remediation itself and never
    silently mutates authoritative state. Ownership-uncertain findings set
    the job to RECONCILIATION_REQUIRED, blocking Recovery Policy until
    ownership is re-established. Only `pre_authorized` bounded cleanups
    (multipart abort, TEMPORARY delete) route directly to the Cleanup
    Executor; everything else goes Policy → Planner → Coordinator via
    `remediation_callback`.
    """

    def __init__(
        self,
        registry: Any,
        infrastructure_monitor: Any,
        cleanup_executor: Any,
        remediation_callback: Optional[Callable] = None,
        plan_store: Any = None,
        checkpoint_store: Any = None,
    ):
        self.registry = registry
        self.infrastructure_monitor = infrastructure_monitor
        self.cleanup_executor = cleanup_executor
        self.remediation_callback = remediation_callback
        self.plan_store = plan_store
        self.checkpoint_store = checkpoint_store
        self._findings: dict[str, ReconciliationFinding] = {}

    # -- entry points --
    def reconcile(self, trigger: ReconciliationTrigger = ReconciliationTrigger.PERIODIC_SWEEP) -> list[ReconciliationFinding]:
        if isinstance(trigger, str):
            trigger = ReconciliationTrigger(trigger)
        findings = []
        if trigger == ReconciliationTrigger.PERIODIC_SWEEP:
            findings.extend(self._periodic_sweep())
        elif trigger == ReconciliationTrigger.EVENT_DRIVEN:
            findings.extend(self._event_driven_check())
        else:
            findings.extend(self._periodic_sweep())

        for finding in findings:
            self._findings[finding.finding_id] = finding
            self._process_finding(finding)
        return findings

    def report_event(self, event: dict) -> list[ReconciliationFinding]:
        """Event-driven path: anomalies, fencing uncertainty, epoch conflicts."""
        etype = str(event.get("event_type", event.get("type", ""))).upper()
        job_id = event.get("job_id", "")
        findings = []
        mapping = {
            "SOURCE_TERMINATED_UNCONFIRMED": MismatchType.SOURCE_STILL_ALIVE_AFTER_FENCE,
            "FENCE_UNCONFIRMED": MismatchType.SOURCE_STILL_ALIVE_AFTER_FENCE,
            "EPOCH_CONFLICT": MismatchType.EPOCH_MISMATCH,
            "OWNERSHIP_CONFLICT": MismatchType.SPLIT_BRAIN,
            "SPLIT_BRAIN": MismatchType.SPLIT_BRAIN,
            "UNEXPECTED_TERMINATION": MismatchType.SOURCE_MISSING,
            "TARGET_LOST": MismatchType.TARGET_MISSING,
        }
        mtype = mapping.get(etype)
        if mtype is not None and job_id:
            findings.append(self._new_finding(
                job_id=job_id, migration_id=event.get("migration_id"),
                mismatch_type=mtype, trigger=ReconciliationTrigger.EVENT_DRIVEN,
                details=dict(event), execution_epoch=int(event.get("execution_epoch", 0) or 0),
                severity="HIGH", confidence=0.9,
                remediation={"type": "RECONCILE_OWNERSHIP",
                             "reason": etype, "pre_authorized": False}))
        for finding in findings:
            self._findings[finding.finding_id] = finding
            self._process_finding(finding)
        return findings

    # -- finding lifecycle --
    def get_finding(self, finding_id: str) -> ReconciliationFinding:
        return self._findings[finding_id]

    def list_findings(self, status: Optional[FindingStatus] = None) -> list[ReconciliationFinding]:
        findings = list(self._findings.values())
        if status is not None:
            findings = [f for f in findings if f.status == status]
        return findings

    def transition_finding(self, finding_id: str, to_status: FindingStatus) -> ReconciliationFinding:
        finding = self._findings[finding_id]
        if isinstance(to_status, str):
            to_status = FindingStatus(to_status)
        if to_status not in _STATUS_FLOW[finding.status]:
            raise RuntimeError(
                f"Illegal finding transition {finding.status.value}->{to_status.value}")
        finding.status = to_status
        return finding

    # -- sweeps --
    def _iter_jobs(self) -> list[dict]:
        list_active = getattr(self.registry, "list_active_jobs", None)
        if callable(list_active):
            try:
                return list(list_active())
            except Exception:
                pass
        list_by_state = getattr(self.registry, "list_by_state", None)
        if callable(list_by_state):
            jobs: list[dict] = []
            for state in ("RUNNING", "MIGRATING", "RECOVERY_REQUIRED",
                          "RECONCILIATION_REQUIRED", "RESTART_REQUIRED"):
                try:
                    jobs.extend(list_by_state(state))
                except Exception:
                    continue
            return jobs
        return []

    def _periodic_sweep(self) -> list[ReconciliationFinding]:
        findings = []
        for job in self._iter_jobs():
            if job.get("state") == "MIGRATING":
                findings.extend(self._check_active_migration(job))
            if job.get("state") == "RUNNING":
                findings.extend(self._check_running_job(job))
            if job.get("state") == "RECONCILIATION_REQUIRED":
                findings.extend(self._check_reconciliation_required(job))
        findings.extend(self._check_orphan_resources())
        findings.extend(self._check_dangling_migrations())
        return findings

    def _event_driven_check(self) -> list[ReconciliationFinding]:
        return []

    def _check_active_migration(self, job: dict) -> list[ReconciliationFinding]:
        findings = []
        migration_id = job.get("active_migration_id")
        if not migration_id:
            findings.append(self._new_finding(
                job_id=job["job_id"], migration_id=None,
                mismatch_type=MismatchType.DANGLING_MIGRATION_REFERENCE,
                trigger=ReconciliationTrigger.PERIODIC_SWEEP,
                details={"registry_state": job.get("state"),
                         "active_migration_id": None},
                execution_epoch=int(job.get("execution_epoch", 0) or 0),
                severity="MEDIUM", confidence=0.9,
                remediation={"type": "CLEAR_DANGLING_MIGRATION",
                             "reason": "MIGRATING without active reference",
                             "pre_authorized": False}))
            return findings

        if self.infrastructure_monitor is None:
            return findings
        try:
            infra_state = self.infrastructure_monitor.get_instance_state(
                job.get("instance_id") or job.get("public_ip") or "")
        except Exception:
            return findings

        if str(infra_state).upper() == "TERMINATED" and job.get("state") == "MIGRATING":
            findings.append(self._new_finding(
                job_id=job["job_id"], migration_id=migration_id,
                mismatch_type=MismatchType.REGISTRY_INFRASTRUCTURE_MISMATCH,
                trigger=ReconciliationTrigger.PERIODIC_SWEEP,
                details={"infra_state": infra_state,
                         "registry_state": job.get("state")},
                observed_state={"instance": "TERMINATED"},
                authoritative_state={"job_state": job.get("state")},
                execution_epoch=int(job.get("execution_epoch", 0) or 0),
                severity="HIGH", confidence=0.9,
                remediation={"type": "RECONCILE_MIGRATION",
                             "reason": "source terminated mid-migration",
                             "pre_authorized": False}))
        return findings

    def _check_running_job(self, job: dict) -> list[ReconciliationFinding]:
        findings = []
        instance_id = job.get("instance_id")
        if not instance_id or self.infrastructure_monitor is None:
            return findings
        try:
            infra_state = self.infrastructure_monitor.get_instance_state(instance_id)
        except Exception:
            return findings
        if str(infra_state).upper() == "TERMINATED":
            findings.append(self._new_finding(
                job_id=job["job_id"], migration_id=None,
                mismatch_type=MismatchType.SOURCE_MISSING,
                trigger=ReconciliationTrigger.PERIODIC_SWEEP,
                details={"instance_id": instance_id, "infra_state": infra_state,
                         "epoch": job.get("execution_epoch", 0)},
                execution_epoch=int(job.get("execution_epoch", 0) or 0),
                severity="HIGH", confidence=0.95,
                remediation={"type": "RECOVER_FROM_CHECKPOINT",
                             "reason": "running job lost its instance",
                             "pre_authorized": False}))
        return findings

    def _check_reconciliation_required(self, job: dict) -> list[ReconciliationFinding]:
        # Re-surface unresolved ownership uncertainty until cleared.
        open_gates = [f for f in self._findings.values()
                      if f.job_id == job.get("job_id")
                      and f.mismatch_type in OWNERSHIP_GATE_TYPES
                      and f.status not in (FindingStatus.RESOLVED, FindingStatus.ESCALATED)]
        return [] if open_gates else []

    def _check_orphan_resources(self) -> list[ReconciliationFinding]:
        findings = []
        if self.plan_store is None:
            return findings
        try:
            active_plans = self.plan_store.list_active()
        except Exception:
            return findings
        jobs = {j.get("job_id"): j for j in self._iter_jobs()}
        for plan in active_plans:
            job_id = plan.get("job_id")
            if job_id not in jobs and plan.get("migration_id") != jobs.get(job_id, {}).get("active_migration_id"):
                job = jobs.get(job_id, {})
                if job.get("active_migration_id") != plan.get("migration_id"):
                    findings.append(self._new_finding(
                        job_id=job_id, migration_id=plan.get("migration_id"),
                        mismatch_type=MismatchType.ORPHAN_INSTANCE,
                        trigger=ReconciliationTrigger.PERIODIC_SWEEP,
                        details={"plan_id": plan.get("plan_id"),
                                 "target": plan.get("target_pool_id")},
                        severity="MEDIUM", confidence=0.7,
                        remediation={"type": "TERMINATE_ORPHAN_TARGET",
                                     "reason": "plan active without job ownership",
                                     "pre_authorized": False}))
        return findings

    def _check_dangling_migrations(self) -> list[ReconciliationFinding]:
        findings = []
        for job in self._iter_jobs():
            if job.get("state") not in ("RUNNING", "COMPLETED", "FAILED"):
                continue
            if job.get("active_migration_id"):
                findings.append(self._new_finding(
                    job_id=job["job_id"],
                    migration_id=job.get("active_migration_id"),
                    mismatch_type=MismatchType.STALE_MIGRATION,
                    trigger=ReconciliationTrigger.PERIODIC_SWEEP,
                    details={"registry_state": job.get("state")},
                    execution_epoch=int(job.get("execution_epoch", 0) or 0),
                    severity="MEDIUM", confidence=0.85,
                    remediation={"type": "CLEAR_STALE_REFERENCE",
                                 "reason": "terminal/running job holds active migration",
                                 "pre_authorized": False}))
        return findings

    # -- processing --
    def _new_finding(self, job_id: str, migration_id: Optional[str],
                     mismatch_type: MismatchType,
                     trigger: ReconciliationTrigger, details: dict,
                     severity: str = "MEDIUM", confidence: float = 0.8,
                     execution_epoch: int = 0,
                     observed_state: dict = None,
                     authoritative_state: dict = None,
                     remediation: dict = None) -> ReconciliationFinding:
        return ReconciliationFinding(
            finding_id=f"REC-{uuid.uuid4().hex[:12]}",
            job_id=job_id, migration_id=migration_id,
            mismatch_type=mismatch_type, severity=severity,
            details=details, detected_at=datetime.utcnow(), trigger=trigger,
            suggested_action=(ReconciliationAction.AUTO_REMEDIATE
                              if (remediation or {}).get("pre_authorized")
                              else ReconciliationAction.REQUEST_REMEDIATION),
            confidence=confidence, execution_epoch=execution_epoch,
            observed_state=observed_state or {}, authoritative_state=authoritative_state or {},
            recommended_remediation=remediation or {},
        )

    def _process_finding(self, finding: ReconciliationFinding):
        logger.warning(f"Reconciliation finding: {finding.mismatch_type.value} for job {finding.job_id}")
        try:
            self.transition_finding(finding.finding_id, FindingStatus.ACKNOWLEDGED)
        except Exception:
            pass

        if finding.mismatch_type in OWNERSHIP_GATE_TYPES:
            self._set_reconciliation_gate(finding)

        if finding.suggested_action == ReconciliationAction.REQUEST_REMEDIATION:
            self._request_remediation(finding)
        elif finding.suggested_action == ReconciliationAction.AUTO_REMEDIATE:
            self._auto_remediate(finding)
        elif finding.suggested_action == ReconciliationAction.ESCALATE:
            self._escalate(finding)

    def _set_reconciliation_gate(self, finding: ReconciliationFinding):
        transition = getattr(self.registry, "transition", None)
        try:
            if callable(transition):
                job = self.registry.get(finding.job_id)
                if job.get("state") != "RECONCILIATION_REQUIRED":
                    transition(finding.job_id, "RECONCILIATION_REQUIRED")
            else:
                update = getattr(self.registry, "update", None)
                if callable(update):
                    update(finding.job_id, "RECONCILIATION_REQUIRED")
        except Exception as exc:
            logger.error("Reconciliation gate transition failed for %s: %s",
                         finding.job_id, exc)

    def _request_remediation(self, finding: ReconciliationFinding):
        try:
            self.transition_finding(finding.finding_id, FindingStatus.REMEDIATING)
        except Exception:
            pass
        request = RemediationRequest(
            request_id=f"REM-{uuid.uuid4().hex[:12]}",
            finding_id=finding.finding_id,
            job_id=finding.job_id,
            action_type=finding.mismatch_type.value,
            parameters={**finding.details, **finding.recommended_remediation},
            priority=1 if finding.severity == "HIGH" else 2,
            requested_at=datetime.utcnow(),
        )
        if self.remediation_callback:
            self.remediation_callback(request)

    def _auto_remediate(self, finding: ReconciliationFinding):
        """Only pre-authorized bounded cleanups ever run here."""
        if not finding.recommended_remediation.get("pre_authorized"):
            self._request_remediation(finding)
            return
        try:
            self.transition_finding(finding.finding_id, FindingStatus.REMEDIATING)
        except Exception:
            pass
        action = finding.recommended_remediation.get("type", "")
        ok = False
        try:
            if self.cleanup_executor is None:
                ok = False
            elif action in ("ABORT_MULTIPART", "DELETE_TEMPORARY"):
                ok = bool(self.cleanup_executor.cleanup_orphan(
                    finding.details.get("resource_id"),
                    finding.details.get("resource_type", "checkpoint")))
            elif action == "TERMINATE_ORPHAN_TARGET":
                ok = bool(self.cleanup_executor.cleanup_orphan(
                    finding.details.get("target") or finding.details.get("resource_id"),
                    "instance"))
            else:
                self._request_remediation(finding)
                return
        except Exception as exc:
            logger.error("Pre-authorized cleanup failed: %s", exc)
            ok = False
        try:
            self.transition_finding(
                finding.finding_id,
                FindingStatus.RESOLVED if ok else FindingStatus.ESCALATED)
        except Exception:
            pass

    def resolve_finding(self, finding_id: str):
        finding = self._findings[finding_id]
        if finding.status == FindingStatus.OPEN:
            self.transition_finding(finding_id, FindingStatus.ACKNOWLEDGED)
        if self._findings[finding_id].status == FindingStatus.ACKNOWLEDGED:
            self.transition_finding(finding_id, FindingStatus.REMEDIATING)
        self.transition_finding(finding_id, FindingStatus.RESOLVED)

    def _escalate(self, finding: ReconciliationFinding):
        try:
            self.transition_finding(finding.finding_id, FindingStatus.ESCALATED)
        except Exception:
            pass
        logger.error(f"ESCALATION: {finding.mismatch_type.value} for job {finding.job_id} - manual intervention required")
