"""Track C1: worker→CPR telemetry path (T1–T4 contract).

Vehicles: C-WORKER (progress durability evidence), C-VALID-adjacent
freshness gating, I7 (missing ≠ favorable) unit evidence.
"""
import json
from datetime import datetime, timezone, timedelta

import pytest

from worker.job_runner import _heartbeat_payload
from orchestrator.worker_telemetry import (
    HEARTBEAT_STALE_SECONDS,
    PREFLIGHT_TTL_SECONDS,
    PROGRESS_STALE_SECONDS,
    TelemetryError,
    WorkerTelemetryReader,
    heartbeat_liveness,
    parse_heartbeat,
    parse_preflight_kv,
    parse_progress,
    parse_refusal,
    preflight_freshness,
    progress_quality,
    refusal_to_finding,
)


NOW = datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc)


def _hb(age_seconds=10, **kw):
    doc = {
        "schema": "heartbeat/v1", "job_id": "job-1", "instance_id": "i-1",
        "execution_epoch": 7, "pid": 4242,
        "observed_at": (NOW - timedelta(seconds=age_seconds)).isoformat(),
        "workload": {"state": "RUNNING", "progress": 0.5},
    }
    doc.update(kw)
    return doc


class _StubTransport:
    def __init__(self, files):
        self.files = files  # name -> stdout text

    def run_command(self, command, timeout=60.0):
        for name, text in self.files.items():
            if name in command:
                return {"returncode": 0, "stdout": text, "stderr": ""}
        return {"returncode": 1, "stdout": "", "stderr": "missing"}

    def close(self):
        return None


# -- T1 emission --
def test_heartbeat_payload_contract_shape(monkeypatch):
    monkeypatch.setenv("WORKLOAD_JOB_ID", "job-7")
    monkeypatch.setenv("INSTANCE_ID", "i-xyz")
    monkeypatch.setenv("WORKLOAD_EXECUTION_EPOCH", "3")
    payload = _heartbeat_payload()
    parsed = parse_heartbeat(payload)  # emission must validate on intake
    assert parsed["job_id"] == "job-7"
    assert parsed["instance_id"] == "i-xyz"
    assert parsed["execution_epoch"] == 3
    assert parsed["pid"] > 0
    assert payload["pid"] == parsed["pid"] and "at" in payload  # back-compat


def test_heartbeat_rejects_bad_schema():
    with pytest.raises(TelemetryError):
        parse_heartbeat({"schema": "heartbeat/v9"})
    with pytest.raises(TelemetryError):
        parse_heartbeat({"schema": "heartbeat/v1"})  # missing identity
    with pytest.raises(TelemetryError):
        parse_heartbeat("not-an-object")


# -- T1 freshness → readiness UNKNOWN, never down --
@pytest.mark.parametrize("age,expected", [
    (10, "READY"), (HEARTBEAT_STALE_SECONDS, "READY"),
    (HEARTBEAT_STALE_SECONDS + 1, "UNKNOWN"), (10_000, "UNKNOWN"),
])
def test_heartbeat_liveness_boundaries(age, expected):
    verdict = heartbeat_liveness(parse_heartbeat(_hb(age_seconds=age)), now=NOW)
    assert verdict["readiness"] == expected
    assert verdict["liveness"] in ("FRESH", "STALE")


def test_missing_heartbeat_is_unknown_never_down():
    verdict = heartbeat_liveness(None, now=NOW)
    assert verdict == {"liveness": "MISSING", "age_seconds": None,
                       "readiness": "UNKNOWN"}
    assert "DOWN" not in json.dumps(verdict)  # I7: no health fabrication


# -- T2 freshness → degraded confidence, still usable --
def test_progress_stale_downgrades_confidence_without_zero_fill():
    fresh = progress_quality({"observed_at": NOW.isoformat(), "progress": 0.5},
                             now=NOW)
    assert fresh == {"quality": "FRESH", "age_seconds": pytest.approx(0),
                     "confidence": "AS_REPORTED", "usable": True}
    stale = progress_quality(
        {"observed_at": (NOW - timedelta(seconds=PROGRESS_STALE_SECONDS + 1)).isoformat()},
        now=NOW)
    assert stale["confidence"] == "LOW" and stale["usable"] is True
    missing = progress_quality(None, now=NOW)
    assert missing == {"quality": "MISSING", "age_seconds": None,
                       "confidence": "LOW", "usable": True}


# -- T3 preflight parse + TTL --
def test_preflight_kv_parse_and_ready_hint():
    text = ("criu_version=3.19\nkernel_version=5.15.0\narchitecture=x86_64\n"
            "capabilities=unprivileged-ok\nunsupported_features=\npassed=true\n")
    rep = parse_preflight_kv(text, pool_id="pool-a", instance_id="i-1",
                             observed_at=NOW)
    assert rep["passed"] is True and rep["criu_version"] == "3.19"
    verdict = preflight_freshness(rep, now=NOW, ttl_seconds=PREFLIGHT_TTL_SECONDS)
    assert verdict == {"fresh": True, "reprobe": False,
                       "age_seconds": pytest.approx(0, abs=5),
                       "ready_hint": "READY"}
    bad = parse_preflight_kv("passed=false\n", pool_id="pool-a")
    assert preflight_freshness(bad, now=NOW)["ready_hint"] == "NOT_READY"
    old = dict(rep, observed_at=NOW - timedelta(seconds=PREFLIGHT_TTL_SECONDS + 1))
    aged = preflight_freshness(old, now=NOW)
    assert aged == {"fresh": False, "reprobe": True,
                    "age_seconds": pytest.approx(PREFLIGHT_TTL_SECONDS + 1),
                    "ready_hint": "UNKNOWN"}
    assert preflight_freshness(None, now=NOW)["reprobe"] is True
    with pytest.raises(TelemetryError):
        parse_preflight_kv("   ", pool_id="pool-a")


# -- T4 refusal → finding --
def test_refusal_to_finding_mapping():
    finding = refusal_to_finding(parse_refusal({
        "schema": "refusal/v1", "instance_id": "i-1", "admission_epoch": 7,
        "operation_id": "op-1", "expected_execution_epoch": 6,
        "reason": "EPOCH_MISMATCH",
        "observed_at": NOW.isoformat()}))
    assert finding["mismatch_type"] == "EPOCH_MISMATCH"
    assert finding["severity"] == "HIGH" and finding["trigger"] == "EVENT_DRIVEN"
    other = refusal_to_finding({"reason": "PAIRING_FAILED"})
    assert other["mismatch_type"] == "UNKNOWN_OWNERSHIP"
    with pytest.raises(TelemetryError):
        parse_refusal({"schema": "refusal/v1", "reason": "X"})  # missing ids


# -- reader over transport (best-effort, never raises) --
def test_reader_round_trip_and_loss_is_none():
    files = {"heartbeat.json": json.dumps(_hb()),
             "progress.json": json.dumps({"progress": 0.25})}
    reader = WorkerTelemetryReader(_StubTransport(files), workspace_root="/w",
                                   clock=lambda: NOW)
    hb = reader.read_heartbeat()
    assert hb["job_id"] == "job-1" and hb["execution_epoch"] == 7
    assert reader.liveness()["readiness"] == "READY"
    assert reader.read_progress()["progress"] == 0.25

    dead = WorkerTelemetryReader(_StubTransport({}), workspace_root="/w",
                                 clock=lambda: NOW)
    assert dead.read_heartbeat() is None
    assert dead.read_progress() is None
    assert dead.liveness()["readiness"] == "UNKNOWN"

    garbage = WorkerTelemetryReader(
        _StubTransport({"heartbeat.json": "not json{"}), workspace_root="/w",
        clock=lambda: NOW)
    assert garbage.read_heartbeat() is None
