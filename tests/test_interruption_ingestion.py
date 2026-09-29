"""Sprint 1: interruption ingestion lane — dedup, deadline-once, stale-checks.

Vehicles: S21 (duplicate interruption → single recovery), S14 (dispatcher
dedup, no double-actuation), §24.1 (deadline computed once at ingestion,
carried immutably — never recomputed from fresh timestamps).
"""
from datetime import datetime, timedelta, timezone

from orchestrator import interruption_ingestion as ing
from orchestrator.deadlines import compute_absolute_deadline
from orchestrator.event_dispatcher import EventConsumer, EventDispatcher
from orchestrator.event_ledger import EventLedger
from orchestrator.protocol import Event, EventType
from orchestrator.reconciliation_manager import (
    MismatchType,
    ReconciliationManager,
)

BASELINE = {
    "execution": {
        "emergency_window_seconds": 120.0,
        "ingestion_skew_margin_seconds": 5.0,
        "wan_allowance_seconds": {
            "default": 15.0,
            "pairs": {"us-east-1/us-west-2": 20.0},
        },
    }
}

T0 = datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc)


def _flag(detected_at=T0, source="imds", instance_id="i-1"):
    return {
        "detected_at": detected_at.isoformat(),
        "source": source,
        "instance_id": instance_id,
    }


def _job(epoch=0):
    return {"job_id": "job-1", "state": "RUNNING", "execution_epoch": epoch,
            "instance_id": "i-1"}


class _Reg:
    def __init__(self):
        self.jobs = {"job-1": dict(_job())}

    def get(self, job_id):
        return dict(self.jobs[job_id])

    def transition(self, job_id, to_state, **kw):
        self.jobs[job_id]["state"] = to_state
        return dict(self.jobs[job_id])

    def list_by_state(self, state):
        return [dict(j) for j in self.jobs.values() if j.get("state") == state]


# -- stable event_id (S21) --
def test_same_notice_same_event_id():
    e1 = ing.ingest_interruption(_flag(), job_id="job-1", baseline=BASELINE)
    e2 = ing.ingest_interruption(_flag(), job_id="job-1", baseline=BASELINE)
    assert e1 is not None and e2 is not None
    assert e1.event_id == e2.event_id  # redelivery converges (S21)


def test_distinct_notices_distinct_ids():
    e1 = ing.ingest_interruption(_flag(T0), job_id="job-1", baseline=BASELINE)
    later = T0 + timedelta(seconds=61)  # next dedup bucket
    e2 = ing.ingest_interruption(_flag(later), job_id="job-1", baseline=BASELINE)
    assert e1.event_id != e2.event_id
    # Same instant, different instance ⇒ different interruption.
    e3 = ing.ingest_interruption(_flag(instance_id="i-2"), job_id="job-1",
                                 baseline=BASELINE)
    assert e3.event_id != e1.event_id


def test_malformed_flag_returns_none():
    assert ing.ingest_interruption(None, job_id="job-1") is None
    assert ing.ingest_interruption("garbage", job_id="job-1") is None
    assert ing.ingest_interruption({}, job_id="job-1") is None
    assert ing.ingest_interruption({"detected_at": "not-a-time"},
                                   job_id="job-1") is None
    assert ing.ingest_interruption(_flag(), job_id="") is None
    assert ing.parse_flag_doc({"detected_at": "2026-13-99"}) is None


# -- deadline computed once at ingestion (§24.1) --
def test_deadline_uses_detected_at_not_now():
    evt = ing.ingest_interruption(
        _flag(), job_id="job-1", source_region="us-east-1",
        target_region="us-west-2", home_region="us-east-1", baseline=BASELINE)
    expected = compute_absolute_deadline(
        T0, 120.0, "us-east-1", "us-west-2", "us-east-1", BASELINE)
    assert expected == T0 + timedelta(seconds=95.0)  # 120 − 20 pair − 5 skew
    assert evt.payload["absolute_deadline"] == expected.isoformat()
    assert evt.effective_at == T0  # ingestion time, not dispatch time
    assert evt.payload["window_seconds"] == 120.0
    assert evt.payload["wan_allowance_seconds"] == 20.0


# -- stale-checks (§8.5) --
def test_fresh_event_is_actionable():
    evt = ing.ingest_interruption(_flag(), job_id="job-1", baseline=BASELINE)
    stale, reason = ing.is_stale_event(evt, _job(epoch=0), now=T0)
    assert (stale, reason) == (False, "fresh")


def test_epoch_mismatch_is_stale():
    evt = ing.ingest_interruption(_flag(), job_id="job-1", baseline=BASELINE,
                                  execution_epoch=0)
    stale, reason = ing.is_stale_event(evt, _job(epoch=1), now=T0)
    assert stale and "epoch-mismatch" in reason


def test_expired_deadline_is_stale():
    evt = ing.ingest_interruption(_flag(), job_id="job-1", baseline=BASELINE)
    stale, reason = ing.is_stale_event(
        evt, _job(), now=T0 + timedelta(seconds=10_000))
    assert stale and "deadline-exceeded" in reason


def test_missing_job_is_stale():
    evt = ing.ingest_interruption(_flag(), job_id="job-1", baseline=BASELINE)
    stale, _ = ing.is_stale_event(evt, None, now=T0)
    assert stale


# -- dispatcher dedup (S14): no double-actuation --
def test_dispatch_same_event_twice_actuates_once():
    dispatcher = EventDispatcher(event_ledger=EventLedger())
    calls = []
    dispatcher.register_consumer(
        "SPOT_INTERRUPTION", EventConsumer("emergency-lane", calls.append))
    evt = ing.ingest_interruption(_flag(), job_id="job-1", baseline=BASELINE)
    dispatcher.dispatch(evt)
    dispatcher.dispatch(evt)  # redelivery ⇒ ledger gate skips
    assert len(calls) == 1


def test_failed_consumer_retries_on_redelivery_then_skips_when_done():
    """S14: FAILED re-arms exactly one attempt per redispatch; COMPLETED skips."""
    import pytest
    dispatcher = EventDispatcher(event_ledger=EventLedger())
    attempts = []

    def _flaky(event):
        attempts.append(event.event_id)
        if len(attempts) == 1:
            raise RuntimeError("transient")
        return None

    dispatcher.register_consumer(
        "SPOT_INTERRUPTION", EventConsumer("emergency-lane", _flaky))
    evt = ing.ingest_interruption(_flag(), job_id="job-1", baseline=BASELINE)
    with pytest.raises(RuntimeError):
        dispatcher.dispatch(evt)  # attempt 1 fails, ledger FAILED
    dispatcher.dispatch(evt)  # redelivery retries (attempt 2 succeeds)
    dispatcher.dispatch(evt)  # COMPLETED ⇒ skipped
    assert attempts == [evt.event_id] * 2


def test_interleaved_types_each_actuate_once():
    """S14: out-of-order delivery across types keeps exactly-once per consumer."""
    dispatcher = EventDispatcher(event_ledger=EventLedger())
    spot_calls, term_calls = [], []
    dispatcher.register_consumer(
        "SPOT_INTERRUPTION", EventConsumer("emergency-lane", spot_calls.append))
    dispatcher.register_consumer(
        "SOURCE_TERMINATED", EventConsumer("fence-watch", term_calls.append))
    spot = ing.ingest_interruption(_flag(), job_id="job-1", baseline=BASELINE)
    term = _source_terminated_event()
    # Arrival order scrambled + duplicates: each (event, consumer) fires once.
    for evt in (term, spot, spot, term, spot):
        dispatcher.dispatch(evt)
    assert len(spot_calls) == 1
    assert len(term_calls) == 1


# -- reconciliation fan-out --
def _source_terminated_event():
    now = datetime.now(timezone.utc)
    return Event(
        event_id="evt-src-term-1", event_type=EventType.SOURCE_TERMINATED,
        job_id="job-1", execution_epoch=0, migration_id="m-1",
        correlation_id="evt-src-term-1", occurred_at=now, effective_at=now,
        producer="test", payload={})


def test_source_terminated_routes_to_reconciliation():
    recon = ReconciliationManager(_Reg(), None, None)
    findings = ing.dispatch_to_reconciliation(_source_terminated_event(), recon)
    assert len(findings) == 1
    assert findings[0].mismatch_type == MismatchType.SOURCE_STILL_ALIVE_AFTER_FENCE


def test_spot_interruption_never_auto_files_finding():
    recon = ReconciliationManager(_Reg(), None, None)
    evt = ing.ingest_interruption(_flag(), job_id="job-1", baseline=BASELINE)
    assert ing.dispatch_to_reconciliation(evt, recon) == []


def test_build_dispatcher_wires_recon_and_late_binds():
    dispatcher = ing.build_dispatcher(EventLedger(), reconciliation=None)
    assert "SOURCE_TERMINATED" in dispatcher.consumers
    recon = ReconciliationManager(_Reg(), None, None)
    dispatcher.set_reconciliation_manager(recon)
    dispatcher.dispatch(_source_terminated_event())
    gated = [f for f in recon.list_findings()
             if f.mismatch_type == MismatchType.SOURCE_STILL_ALIVE_AFTER_FENCE]
    assert len(gated) == 1
