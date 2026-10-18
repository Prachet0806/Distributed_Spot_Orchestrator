"""Validation shared by the V2 registry backends; no record upgrades."""
from decimal import Decimal
from storage.v2_transitions import JOB_TRANSITIONS


def validate_job_record(record):
    required = {"job_id", "state", "version", "execution_epoch", "active_migration_id"}
    missing = required - record.keys()
    if missing:
        raise ValueError(f"V2 job record missing fields: {', '.join(sorted(missing))}")
    if not record["job_id"] or record["state"] not in JOB_TRANSITIONS:
        raise ValueError("Unsupported job identity or lifecycle state")
    if record.get("engine_version", "v2") != "v2":
        raise ValueError("Only V2 execution records are supported")
    for key in ("version", "execution_epoch"):
        value = record[key]
        if not isinstance(value, (int, Decimal)) or isinstance(value, bool) or value < 0 or int(value) != value:
            raise ValueError(f"{key} must be a nonnegative integer")
    active = record["active_migration_id"]
    if active is not None and (not isinstance(active, str) or not active):
        raise ValueError("active_migration_id must be a nonempty string or None")
    return record
