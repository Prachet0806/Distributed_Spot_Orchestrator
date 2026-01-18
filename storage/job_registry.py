# storage/job_registry.py
import json
from threading import Lock

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
            data[job_id]["state"] = state
            data[job_id].update(kwargs)
            self._save(data)
