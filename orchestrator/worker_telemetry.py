# orchestrator/worker_telemetry.py — worker → control-plane evidence (Track C1).
"""CPR intake for the frozen telemetry contract (docs/telemetry-contract.md).

Workers emit evidence only; emission never triggers decisions locally.
Transport is best-effort file reads over the existing WorkerTransport
(SSH on AWS, emulated locally) — loss degrades confidence, never fabricates
health. Freshness rules:

- heartbeat ≤ 60 s ⇒ FRESH; else STALE → readiness UNKNOWN (never down).
- progress ≤ 5 min ⇒ FRESH; else estimator confidence downgraded, never zero.
- preflight ≤ TTL (1 h) ⇒ FRESH; else re-probe before admission.
- refusal ⇒ always actionable, filed immediately, never batched.

Main-loop wiring (replacing the hardcoded readiness evidence in
`orchestrator/main.py`) lands with the Dynamo/runtime wiring (Track B1):
construct a WorkerTelemetryReader per source host and feed
`readiness_liveness()` into the readiness assessment. Until then this
module is exercised by tests + the emulated path.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

HEARTBEAT_SCHEMA = "heartbeat/v1"
PROGRESS_SCHEMA = "progress/v1"
PREFLIGHT_SCHEMA = "preflight/v1"
REFUSAL_SCHEMA = "refusal/v1"

HEARTBEAT_STALE_SECONDS = 60.0
PROGRESS_STALE_SECONDS = 300.0
PREFLIGHT_TTL_SECONDS = 3600.0

# T4 reason → reconciliation mismatch type (Protocols #11 §13.3).
REFUSAL_FINDING_MAP = {
    "EPOCH_MISMATCH": "EPOCH_MISMATCH",
    "UNKNOWN_OPERATION": "UNKNOWN_OWNERSHIP",
    "PAIRING_FAILED": "UNKNOWN_OWNERSHIP",
}


class TelemetryError(ValueError):
    """Report fails schema validation — dropped, never fabricated."""


def _parse_time(value: Any, field: str) -> datetime:
    if isinstance(value, datetime):
        ts = value
    elif isinstance(value, str) and value:
        try:
            ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as e:
            raise TelemetryError(f"bad timestamp in {field}: {value!r}") from e
    else:
        raise TelemetryError(f"missing timestamp in {field}")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _require_schema(obj: Dict[str, Any], schema: str) -> Dict[str, Any]:
    if not isinstance(obj, dict):
        raise TelemetryError(f"{schema}: report is not an object")
    if obj.get("schema") != schema:
        raise TelemetryError(
            f"expected schema {schema}, got {obj.get('schema')!r}")
    return obj


def parse_heartbeat(obj: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a T1 heartbeat/v1 report."""
    doc = _require_schema(obj, HEARTBEAT_SCHEMA)
    for key in ("job_id", "instance_id", "execution_epoch", "pid"):
        if doc.get(key) is None:
            raise TelemetryError(f"heartbeat/v1 missing {key}")
    return {
        "schema": HEARTBEAT_SCHEMA,
        "job_id": str(doc["job_id"]),
        "instance_id": str(doc["instance_id"]),
        "execution_epoch": int(doc["execution_epoch"]),
        "pid": int(doc["pid"]),
        "observed_at": _parse_time(
            doc.get("observed_at", doc.get("at")), "heartbeat/v1"),
        "workload": doc.get("workload") or {},
    }


def parse_progress(obj: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a T2 progress/v1 report (durable progress-file content)."""
    if not isinstance(obj, dict):
        raise TelemetryError("progress/v1: report is not an object")
    if obj.get("schema") is not None and obj.get("schema") != PROGRESS_SCHEMA:
        raise TelemetryError(f"unexpected schema {obj.get('schema')!r}")
    return {
        "schema": PROGRESS_SCHEMA,
        "job_id": obj.get("job_id"),
        "execution_epoch": obj.get("execution_epoch"),
        "observed_at": _parse_time(
            obj.get("observed_at", obj.get("at",
                                           datetime.now(timezone.utc).isoformat())),
            "progress/v1"),
        "progress": obj.get("progress"),
        "checkpoint_size_bytes": obj.get("checkpoint_size_bytes"),
        "checkpoint_duration_seconds": obj.get("checkpoint_duration_seconds"),
        "observation_confidence": obj.get("observation_confidence"),
        "raw": {k: v for k, v in obj.items()
                if k not in ("schema", "observed_at", "at")},
    }


def parse_preflight_kv(text: str, pool_id: str,
                       instance_id: str = "",
                       observed_at: Optional[datetime] = None) -> Dict[str, Any]:
    """Parse `criu_wrapper.sh preflight` key=value output → T3 report."""
    if not isinstance(text, str) or not text.strip():
        raise TelemetryError("preflight: empty output")
    kv: Dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            kv[key.strip()] = value.strip()
    passed_raw = kv.get("passed", "").lower()
    return {
        "schema": PREFLIGHT_SCHEMA,
        "pool_id": pool_id,
        "instance_id": instance_id,
        "criu_version": kv.get("criu_version", ""),
        "kernel_version": kv.get("kernel_version", ""),
        "architecture": kv.get("architecture", ""),
        "capabilities": kv.get("capabilities", ""),
        "unsupported_features": kv.get("unsupported_features", ""),
        "passed": passed_raw in ("true", "1", "yes"),
        "observed_at": observed_at or datetime.now(timezone.utc),
    }


def parse_refusal(obj: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a T4 command-refusal report (worker-controller trust, D3)."""
    doc = _require_schema(obj, REFUSAL_SCHEMA)
    for key in ("instance_id", "operation_id", "reason"):
        if doc.get(key) is None:
            raise TelemetryError(f"refusal/v1 missing {key}")
    return {
        "schema": REFUSAL_SCHEMA,
        "instance_id": str(doc["instance_id"]),
        "admission_epoch": doc.get("admission_epoch"),
        "operation_id": str(doc["operation_id"]),
        "expected_execution_epoch": doc.get("expected_execution_epoch"),
        "reason": str(doc["reason"]),
        "observed_at": _parse_time(
            doc.get("observed_at", datetime.now(timezone.utc).isoformat()),
            "refusal/v1"),
    }


def _age_seconds(observed_at: datetime, now: datetime) -> float:
    return (now - observed_at).total_seconds()


def _observed_at_of(report: Dict[str, Any], stream: str) -> datetime:
    """Accept parsed (datetime) or raw (ISO string) reports from any caller."""
    return _parse_time(report.get("observed_at", report.get("at")), stream)


def heartbeat_liveness(report: Optional[Dict[str, Any]],
                       now: Optional[datetime] = None) -> Dict[str, Any]:
    """T1 freshness → liveness. STALE/MISSING ⇒ readiness UNKNOWN (never down)."""
    now = now or datetime.now(timezone.utc)
    if report is None:
        return {"liveness": "MISSING", "age_seconds": None,
                "readiness": "UNKNOWN"}
    age = _age_seconds(_observed_at_of(report, "heartbeat"), now)
    if age <= HEARTBEAT_STALE_SECONDS:
        return {"liveness": "FRESH", "age_seconds": age, "readiness": "READY",
                "execution_epoch": report.get("execution_epoch"),
                "pid": report.get("pid")}
    return {"liveness": "STALE", "age_seconds": age, "readiness": "UNKNOWN"}


def progress_quality(report: Optional[Dict[str, Any]],
                     now: Optional[datetime] = None) -> Dict[str, Any]:
    """T2 freshness → estimator confidence hint (never zero-fill)."""
    now = now or datetime.now(timezone.utc)
    if report is None:
        return {"quality": "MISSING", "age_seconds": None,
                "confidence": "LOW", "usable": True}
    try:
        age = _age_seconds(_observed_at_of(report, "progress"), now)
    except TelemetryError:
        return {"quality": "MISSING", "age_seconds": None,
                "confidence": "LOW", "usable": True}
    if age <= PROGRESS_STALE_SECONDS:
        return {"quality": "FRESH", "age_seconds": age,
                "confidence": "AS_REPORTED", "usable": True}
    return {"quality": "STALE", "age_seconds": age,
            "confidence": "LOW", "usable": True}


def preflight_freshness(report: Optional[Dict[str, Any]],
                        now: Optional[datetime] = None,
                        ttl_seconds: float = PREFLIGHT_TTL_SECONDS) -> Dict[str, Any]:
    """T3 cache status → re-probe decision before admission."""
    now = now or datetime.now(timezone.utc)
    if report is None:
        return {"fresh": False, "reprobe": True, "ready_hint": "UNKNOWN"}
    age = _age_seconds(_observed_at_of(report, "preflight"), now)
    if age <= ttl_seconds:
        return {"fresh": True, "reprobe": False, "age_seconds": age,
                "ready_hint": "READY" if report.get("passed") else "NOT_READY"}
    return {"fresh": False, "reprobe": True, "age_seconds": age,
            "ready_hint": "UNKNOWN"}


def refusal_to_finding(refusal: Dict[str, Any]) -> Dict[str, Any]:
    """T4 refusal → reconciliation finding input (filed immediately)."""
    reason = str(refusal.get("reason", ""))
    return {
        "mismatch_type": REFUSAL_FINDING_MAP.get(reason, "UNKNOWN_OWNERSHIP"),
        "severity": "HIGH",
        "trigger": "EVENT_DRIVEN",
        "evidence_refs": [refusal],
        "job_id": refusal.get("job_id"),
        "execution_epoch": refusal.get("expected_execution_epoch"),
    }


class WorkerTelemetryReader:
    """Best-effort file reads over a WorkerTransport (never raising).

    Transport loss ⇒ None (degraded confidence downstream). Used with
    EmulatedWorkerTransport in tests/emulation and SSHWorkerTransport on AWS.
    """

    def __init__(self, transport: Any, workspace_root: str = "/opt/job_workspace",
                 clock: Optional[Callable[[], datetime]] = None):
        self.transport = transport
        self.workspace_root = workspace_root.rstrip("/")
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _read_json(self, name: str) -> Optional[Dict[str, Any]]:
        import json
        try:
            res = self.transport.run_command(
                f"cat {self.workspace_root}/{name}", timeout=15.0)
        except Exception:
            return None
        if not isinstance(res, dict) or res.get("returncode", 1) != 0:
            return None
        try:
            obj = json.loads(res.get("stdout", ""))
        except Exception:
            return None
        return obj if isinstance(obj, dict) else None

    def read_heartbeat(self) -> Optional[Dict[str, Any]]:
        try:
            return parse_heartbeat(self._read_json("heartbeat.json"))
        except (TelemetryError, TypeError):
            return None

    def read_progress(self) -> Optional[Dict[str, Any]]:
        try:
            return parse_progress(self._read_json("progress.json"))
        except (TelemetryError, TypeError):
            return None

    def run_preflight(self, pool_id: str, instance_id: str = "",
                      wrapper_path: str = "/opt/job_workspace/criu_wrapper.sh"
                      ) -> Optional[Dict[str, Any]]:
        try:
            res = self.transport.run_command(
                f"{wrapper_path} preflight", timeout=60.0)
        except Exception:
            return None
        if not isinstance(res, dict):
            return None
        try:
            return parse_preflight_kv(res.get("stdout", ""), pool_id,
                                      instance_id)
        except TelemetryError:
            return None

    def liveness(self) -> Dict[str, Any]:
        """Combined T1 freshness verdict for the readiness assessment."""
        return heartbeat_liveness(self.read_heartbeat(), now=self._clock())
