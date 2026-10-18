"""Strict V2 registry for emulation. It does not load legacy JSON files."""
from copy import deepcopy
from datetime import datetime, timezone
from threading import RLock

from storage.registry_contract import validate_job_record
from storage.v2_transitions import is_v2_transition_allowed


class MemoryRegistry:
    def __init__(self, records=None):
        self._records = {}
        self._lock = RLock()
        for job_id, record in (records or {}).items():
            if record.get("job_id") != job_id:
                raise ValueError("Registry key must match job_id")
            self._records[job_id] = deepcopy(validate_job_record(record))

    def create(self, job_id, **attrs):
        with self._lock:
            if job_id in self._records:
                raise KeyError(f"job_id {job_id} already exists")
            record = {
                "job_id": job_id, "state": "PENDING", "version": 0,
                "execution_epoch": 0, "active_migration_id": None,
                "last_updated": datetime.now(timezone.utc).isoformat(),
                **attrs,
            }
            if record["job_id"] != job_id:
                raise ValueError("Registry key must match job_id")
            self._records[job_id] = deepcopy(validate_job_record(record))
            return deepcopy(record)

    def get(self, job_id):
        with self._lock:
            return deepcopy(self._records[job_id])

    def list_by_state(self, state):
        with self._lock:
            return [deepcopy(row) for row in self._records.values()
                    if row["state"] == state]

    def transition(self, job_id, to_state, expected_version=None,
                   expected_epoch=None, ownership_change=False,
                   active_migration_id=None, clear_active_migration=False,
                   **attrs):
        with self._lock:
            record = self.get(job_id)
            current = record["state"]
            allowed = is_v2_transition_allowed("job", current, to_state)
            if current == to_state and (ownership_change or current == "PENDING"):
                allowed = True
            if not allowed:
                raise RuntimeError(f"Invalid state transition: {current} -> {to_state}")
            if expected_version is not None and expected_version != record["version"]:
                raise RuntimeError(f"Optimistic lock failed for job_id {job_id}")
            if expected_epoch is not None and expected_epoch != record["execution_epoch"]:
                raise RuntimeError(f"Epoch conflict for {job_id}")
            active = record["active_migration_id"]
            if to_state == "MIGRATING":
                if active and active_migration_id and active != active_migration_id:
                    raise RuntimeError(f"Job {job_id} already has active migration {active}")
                active = active_migration_id or active
            elif active_migration_id is not None:
                active = active_migration_id
            if clear_active_migration or (current == "MIGRATING" and
                    to_state in {"RUNNING", "COMPLETED", "FAILED"}):
                active = None
            record.update({k: deepcopy(v) for k, v in attrs.items()
                           if k not in {"version", "execution_epoch", "active_migration_id"}})
            record.update(state=to_state, version=record["version"] + 1,
                          execution_epoch=record["execution_epoch"] + int(ownership_change),
                          active_migration_id=active,
                          last_updated=datetime.now(timezone.utc).isoformat())
            self._records[job_id] = validate_job_record(record)
            return deepcopy(record)
