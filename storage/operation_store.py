"""Write-before-command journal. An uncertain operation is never reissued blindly."""
from datetime import datetime, timezone
from storage.backend import Documents, encode, decode, conditional_failure


class OperationStore:
    def __init__(self, table_name="spot_arbitrage_operations", dynamodb_resource=None):
        self._docs = Documents("operation_key", table_name, dynamodb_resource)

    def begin(self, operation_key, request):
        from orchestrator.models_v2 import new_ulid_like
        request = decode(encode(request))
        with self._docs.lock:
            try:
                current = self._docs.get(operation_key)
            except KeyError:
                current = None
            if current is not None:
                if current["request"] != request:
                    raise RuntimeError("operation identity reused for a different request")
                return {**current, "newly_issued": False}
            doc = {"operation_key": operation_key, "operation_id": new_ulid_like(),
                   "request": request, "state": "ISSUED", "version": 0,
                   "issued_at": datetime.now(timezone.utc).isoformat()}
            try:
                return {**self._docs.put(doc, create=True), "newly_issued": True}
            except Exception as exc:
                if conditional_failure(exc):
                    current = self._docs.get(operation_key)
                    if current["request"] != request:
                        raise RuntimeError("operation request conflict") from exc
                    return {**current, "newly_issued": False}
                raise

    def get(self, operation_id):
        matches = [r for r in self._docs.all() if r["operation_id"] == operation_id]
        if not matches:
            raise KeyError(operation_id)
        return matches[0]

    def finish(self, operation_id, state, *, result=None, error=None):
        if state not in {"SUCCEEDED", "FAILED", "UNKNOWN"}:
            raise ValueError(state)
        with self._docs.lock:
            doc = self.get(operation_id)
            if doc["state"] in {"SUCCEEDED", "FAILED"}:
                if doc["state"] == state:
                    return doc
                raise RuntimeError("terminal operation outcome conflict")
            version = doc["version"]
            doc.update(state=state, result=decode(encode(result)), error=error,
                       version=version + 1, completed_at=datetime.now(timezone.utc).isoformat())
            return self._docs.put(doc, expected_version=version)

    def get_operation_status(self, operation_id):
        try:
            return self.get(operation_id)
        except KeyError:
            return {"operation_id": operation_id, "state": "UNKNOWN"}
