"""Write-before-command journal. An uncertain operation is never reissued blindly."""

from datetime import datetime, timezone

from storage.backend import Documents, conditional_failure, decode, encode


class OperationStore:
    def __init__(self, table_name="spot_arbitrage_operations", dynamodb_resource=None):
        self._docs = Documents("operation_key", table_name, dynamodb_resource)
        self._keys_by_id = {}

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
                    raise RuntimeError(
                        "operation identity reused for a different request"
                    )
                self._keys_by_id[current["operation_id"]] = operation_key
                return {**current, "newly_issued": False}
            doc = {
                "operation_key": operation_key,
                "operation_id": new_ulid_like(),
                "request": request,
                "state": "ISSUED",
                "version": 0,
                "issued_at": datetime.now(timezone.utc).isoformat(),
            }
            try:
                saved = self._docs.put(doc, create=True)
                self._keys_by_id[saved["operation_id"]] = operation_key
                return {**saved, "newly_issued": True}
            except Exception as exc:
                if conditional_failure(exc):
                    current = self._docs.get(operation_key)
                    if current["request"] != request:
                        raise RuntimeError("operation request conflict") from exc
                    self._keys_by_id[current["operation_id"]] = operation_key
                    return {**current, "newly_issued": False}
                raise

    def get(self, operation_id):
        key = self._keys_by_id.get(operation_id)
        if key is not None:
            doc = self._docs.get(key)
            if doc["operation_id"] == operation_id:
                return doc
        matches = self._docs.query_index("OperationIdIndex", "operation_id", operation_id)
        if not matches:
            raise KeyError(operation_id)
        self._keys_by_id[operation_id] = matches[0]["operation_key"]
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
            doc.update(
                state=state,
                result=decode(encode(result)),
                error=error,
                version=version + 1,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            return self._docs.put(doc, expected_version=version)

    def get_operation_status(self, operation_id):
        try:
            return self.get(operation_id)
        except KeyError:
            return {"operation_id": operation_id, "state": "UNKNOWN"}
