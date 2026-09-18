# storage/job_registry.py
"""JSON registry — V1 compat path (frozen except V2 delegation).

New code should use DynamoRegistry.transition() / V2 transition table.
Kept runnable for `--engine v1`, scripts/registry_cli.py, and emulated
tests until ADR-024 deletion gates pass."""
import warnings

warnings.warn(
    "storage.job_registry.JobRegistry is V1 compat (frozen). "
    "Prefer V2 Registry.transition().",
    DeprecationWarning,
    stacklevel=2,
)
import json
from threading import Lock
from datetime import datetime
from storage.job_states import is_transition_allowed

class JobRegistry:
    def __init__(self, path="storage/job_registry.json"):
        self.path = path
        self.lock = Lock()

    def _load(self):
        with open(self.path) as f:
            return json.load(f)

    def _save(self, data):
        with open(self.path, "w") as f:
            json.dump(data, f, indent=2)

    def get(self, job_id):
        return self._load()[job_id]

    def update(self, job_id, state, **kwargs):
        with self.lock:
            data = self._load()
            if job_id not in data:
                raise KeyError(f"job_id {job_id} not found")
            current_state = data[job_id].get("state")
            if not is_transition_allowed(current_state, state):
                raise RuntimeError(
                    f"Invalid state transition: {current_state} -> {state} for {job_id}"
                )
            data[job_id]["state"] = state
            data[job_id].update(kwargs)
            data[job_id]["last_updated"] = datetime.utcnow().isoformat()
            self._save(data)

    def transition(self, job_id, to_state, expected_version=None,
                   expected_epoch=None, ownership_change=False,
                   active_migration_id=None, clear_active_migration=False,
                   **attrs):
        """V2 sole mutation path for the JSON backend (file-local CAS).

        Mirrors DynamoRegistry.transition semantics so the Coordinator can
        run unchanged on either backend in emulated E2E.
        """
        from storage.v2_transitions import is_v2_transition_allowed

        with self.lock:
            data = self._load()
            if job_id not in data:
                raise KeyError(f"job_id {job_id} not found")
            item = data[job_id]
            current_state = item.get("state")
            v2_ok = is_v2_transition_allowed("job", current_state, to_state)
            if to_state == current_state and ownership_change:
                # Epoch rotation (fencing invalidation) without lifecycle move.
                v2_ok = True
            try:
                legacy_ok = is_transition_allowed(current_state, to_state)
            except ValueError:
                legacy_ok = False
            if not (v2_ok or legacy_ok):
                raise RuntimeError(
                    f"Invalid state transition: {current_state} -> {to_state} for {job_id}"
                )
            current_version = expected_version
            if current_version is None:
                current_version = item.get("version", 0)
            current_epoch = item.get("execution_epoch", 0) or 0
            if item.get("version", 0) != (current_version or 0):
                raise RuntimeError(f"Optimistic lock failed for job_id {job_id}")
            if expected_epoch is not None and expected_epoch != current_epoch:
                raise RuntimeError(f"Epoch conflict for {job_id}")
            new_version = (current_version or 0) + 1
            new_epoch = current_epoch + (1 if ownership_change else 0)

            current_active = item.get("active_migration_id")
            new_active = current_active
            if to_state == "MIGRATING":
                want = active_migration_id or attrs.get("active_migration_id")
                if current_active and want and current_active != want:
                    raise RuntimeError(
                        f"Job {job_id} already has active migration {current_active}")
                new_active = want or current_active
            if clear_active_migration or (
                    to_state in ("RUNNING", "COMPLETED", "FAILED")
                    and current_state == "MIGRATING"):
                new_active = None
            elif active_migration_id is not None and to_state != "MIGRATING":
                new_active = active_migration_id

            item["state"] = to_state
            item["version"] = new_version
            item["execution_epoch"] = new_epoch
            item["active_migration_id"] = new_active
            for k, v in attrs.items():
                if k not in ("active_migration_id", "execution_epoch", "version"):
                    item[k] = v
            item["last_updated"] = datetime.utcnow().isoformat()
            self._save(data)
            return dict(item)
