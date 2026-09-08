# storage/v2_transitions.py — V2 four-domain transition tables (Protocols §0.4/§0.6).
"""Additive. V1 `job_states.ALLOWED_TRANSITIONS` stays frozen for legacy path."""

JOB_TRANSITIONS: dict[str, set[str]] = {
    "REGISTERED": {"READY", "FAILED"},
    "READY": {"RUNNING", "FAILED"},
    "RUNNING": {"MIGRATING", "RECONCILIATION_REQUIRED", "RECOVERY_REQUIRED", "RESTART_REQUIRED", "COMPLETED", "FAILED"},
    "MIGRATING": {"RUNNING", "RECONCILIATION_REQUIRED", "RECOVERY_REQUIRED", "RESTART_REQUIRED", "COMPLETED", "FAILED"},
    "RECONCILIATION_REQUIRED": {"RUNNING", "MIGRATING", "RECOVERY_REQUIRED", "RESTART_REQUIRED", "FAILED"},
    "RECOVERY_REQUIRED": {"MIGRATING", "RESTART_REQUIRED", "FAILED"},
    "RESTART_REQUIRED": {"READY", "FAILED"},
    "COMPLETED": set(),
    "FAILED": set(),
}

MIGRATION_TRANSITIONS: dict[str, set[str]] = {
    "PLANNED": {"PRECHECKING", "ABORTED", "SUPERSEDED", "FAILED"},
    "PRECHECKING": {"CHECKPOINTING", "PROVISIONING", "ABORTED", "SUPERSEDED", "FAILED"},
    "CHECKPOINTING": {"PERSISTING", "ABORTED", "SUPERSEDED", "FAILED"},
    "PERSISTING": {"PROVISIONING", "TRANSFERRING", "ABORTED", "SUPERSEDED", "FAILED"},
    "PROVISIONING": {"TRANSFERRING", "ABORTED", "SUPERSEDED", "FAILED"},
    "TRANSFERRING": {"RESTORING", "ABORTED", "SUPERSEDED", "FAILED"},
    "RESTORING": {"FENCING", "ABORTED", "SUPERSEDED", "FAILED"},
    "FENCING": {"VALIDATING", "FAILED"},  # no ABORT mid-fence: COMPLETE_FENCING
    "VALIDATING": {"ACTIVATING", "FAILED", "SUPERSEDED"},
    "ACTIVATING": {"FINALIZING", "FAILED", "SUPERSEDED"},
    "FINALIZING": {"SUCCESS", "FAILED", "SUPERSEDED"},
    "SUCCESS": set(),
    "ABORTED": set(),
    "SUPERSEDED": set(),
    "FAILED": set(),
}

PLANSTEP_TRANSITIONS: dict[str, set[str]] = {
    "PENDING": {"RUNNING", "SKIPPED", "FAILED"},
    "RUNNING": {"SUCCEEDED", "FAILED", "UNKNOWN"},
    "UNKNOWN": {"SUCCEEDED", "FAILED", "RUNNING"},
    "SUCCEEDED": set(),
    "FAILED": set(),
    "SKIPPED": set(),
}

OPERATION_TRANSITIONS: dict[str, set[str]] = {
    "ISSUED": {"RUNNING", "FAILED", "UNKNOWN"},
    "RUNNING": {"SUCCEEDED", "FAILED", "UNKNOWN"},
    "UNKNOWN": {"SUCCEEDED", "FAILED", "RUNNING"},
    "SUCCEEDED": set(),
    "FAILED": set(),
}

_TABLES = {
    "job": JOB_TRANSITIONS,
    "migration": MIGRATION_TRANSITIONS,
    "planstep": PLANSTEP_TRANSITIONS,
    "operation": OPERATION_TRANSITIONS,
}


def is_v2_transition_allowed(domain: str, current: str | None, nxt: str | None) -> bool:
    table = _TABLES.get(domain)
    if table is None or current is None or nxt is None:
        return False
    cur, nx = str(current), str(nxt)
    if cur == nx:
        return cur in ("RUNNING",)  # heartbeat re-assert only for job RUNNING
    return nx in table.get(cur, set())
