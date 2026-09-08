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
