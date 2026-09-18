"""Track C5: §10.8 step retry/backoff/priority + replan/supersession + dedup.

Vehicles: S04 (deadline→abort/replan), S15/S16 (replan/supersession),
C-PLAN (hash/pins), C-ID (operation idempotency across retries).
"""
import json
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from orchestrator.migration_planner import MigrationPlanner, MigrationState
from orchestrator.migration_coordinator import (
    MigrationCoordinator, MigrationFailed, OperationUnknownError,
)
from orchestrator.models_v2 import Criticality
from orchestrator.policy_engine import PolicyDecision, Decision, MigrationRegime
from orchestrator.step_retry import (
    CodedError,
    backoff_delay_seconds,
    failure_code_of,
    reject_injection_points,
    schedule_order,
    should_retry,
    spec_for,
)
from orchestrator.workload_estimator import WorkloadEstimate


# -- fixtures --
def _est(remaining=7200):
    return WorkloadEstimate(
        job_id="job-1", execution_epoch=0, progress=0.5,
        remaining_runtime_seconds=remaining, expected_completion_at=None,
        prediction_confidence=0.8, checkpoint_size_estimate_bytes=100,
        checkpoint_duration_estimate_seconds=1.0,
        estimated_at=datetime.utcnow(), estimator_version="v",
        model_version="v", observation_snapshot_version="1")


def _decision():
    return PolicyDecision(decision=Decision.MIGRATE, regime=MigrationRegime.ARBITRAGE,
                          reason="t", target_candidate_id="pool-t",
                          confidence=0.9, metadata={})


def _plan(**kw):
    return MigrationPlanner().create_plan(
        job_id="job-1", execution_epoch=0, regime=MigrationRegime.ARBITRAGE,
        policy_decision=_decision(), source_pool_id="pool-s",
        target_pool_id="pool-t", workload_estimate=_est(), **kw)


def _coord(sleeps, **kw):
    kw.setdefault("sleep_fn", sleeps.append)
    kw.setdefault("step_timeout_seconds", 30.0)
    return MigrationCoordinator(
        registry=SimpleNamespace(get=lambda j: None,
                                 transition=lambda *a, **k: None),
        provisioner=SimpleNamespace(), checkpoint_manager=SimpleNamespace(),
        transfer_manager=SimpleNamespace(), validator=SimpleNamespace(),
        cleanup_executor=SimpleNamespace(), **kw)


def _seed(coord, plan):
    from orchestrator.migration_coordinator import MigrationExecutionState
    coord._execution_state = MigrationExecutionState(
        plan=plan, current_state=MigrationState.CHECKPOINTING,
        state_entered_at=datetime.utcnow(), execution_epoch=0,
        expected_epoch=0, checkpoint_id=None)
    coord._start_mono = coord._monotonic()
    return coord


# -- §10.8 rule 1+2: budget + retryable codes --
def test_attempt_budget_exhausts_then_raises():
    sleeps, calls = [], []

    def fail():
        calls.append(1)
        raise CodedError("CRIU_DUMP_FAILED", "dump blew up", transience="TRANSIENT")

    coord = _seed(_coord(sleeps), _plan())
    with pytest.raises(CodedError):
        coord._run_operation_with_retry(
            step_type="CHECKPOINT", operation_id="op-1", func=fail,
            timeout=30.0, default_code="CRIU_DUMP_FAILED")
    assert len(calls) == 3  # ADR-012 default max_attempts
    assert len(sleeps) == 2  # backoff between attempts only


def test_unlisted_code_escalates_immediately():
    sleeps, calls = [], []

    def fail():
        calls.append(1)
        raise CodedError("SOME_WEIRD_CODE", "unknown failure")

    coord = _seed(_coord(sleeps), _plan())
    with pytest.raises(CodedError):
        coord._run_operation_with_retry(
            step_type="CHECKPOINT", operation_id="op-1", func=fail,
            timeout=30.0, default_code="CRIU_DUMP_FAILED")
    assert len(calls) == 1 and sleeps == []


def test_uncoded_error_escalates_immediately():
    calls = []

    def fail():
        calls.append(1)
        raise RuntimeError("bare executor error")

    coord = _seed(_coord([]), _plan())
    with pytest.raises(RuntimeError):
        coord._run_operation_with_retry(
            step_type="PROVISION", operation_id="op-1", func=fail,
            timeout=30.0, default_code="PROVISION_FAILED")
    assert len(calls) == 1


def test_should_retry_needs_code_budget_and_attempts():
    spec = spec_for("TRANSFER")
    assert should_retry(spec, failure_code="TRANSFER_TIMEOUT",
                        attempts_used=1, budgets_valid=True)
    assert not should_retry(spec, failure_code="TRANSFER_TIMEOUT",
                            attempts_used=3, budgets_valid=True)
    assert not should_retry(spec, failure_code="TRANSFER_TIMEOUT",
                            attempts_used=1, budgets_valid=False)
    assert not should_retry(spec, failure_code="NOPE",
                            attempts_used=1, budgets_valid=True)
    assert not should_retry(spec, failure_code=None,
                            attempts_used=1, budgets_valid=True)
    # Verdict/ownership steps never step-retry: escalate to Recovery Policy.
    assert not should_retry(spec_for("VALIDATE"), failure_code="VALIDATION_FAILED",
                            attempts_used=1, budgets_valid=True)
    assert not should_retry(spec_for("FENCE"), failure_code="FENCING_FAILED",
                            attempts_used=1, budgets_valid=True)


def test_failure_code_of_prefers_coded_prefers_default():
    assert failure_code_of(CodedError("X", "m"), "D") == "X"
    assert failure_code_of(ValueError("m"), "D") == "D"


# -- §10.8 rule 3: backoff shape + deadline consumption --
def test_backoff_grows_and_caps_with_jitter_bounds():
    spec = spec_for("CHECKPOINT")
    mid = lambda lo, hi: (lo + hi) / 2  # noqa: E731 — deterministic midpoint
    assert backoff_delay_seconds(spec, 1, rng=mid) == pytest.approx(2.0)
    assert backoff_delay_seconds(spec, 2, rng=mid) == pytest.approx(4.0)
    assert backoff_delay_seconds(spec, 3, rng=mid) == pytest.approx(8.0)
    assert backoff_delay_seconds(spec, 99, rng=mid) == pytest.approx(30.0)
    lo = lambda l, h: l  # noqa: E731
    hi = lambda l, h: h  # noqa: E731
    assert backoff_delay_seconds(spec, 1, rng=lo) == pytest.approx(1.5)
    assert backoff_delay_seconds(spec, 1, rng=hi) == pytest.approx(2.5)


def test_backoff_exceeding_deadline_raises_instead_of_sleeping():
    from orchestrator.migration_coordinator import DeadlineExceeded
    sleeps = []
    plan = MigrationPlanner().create_plan(
        job_id="job-1", execution_epoch=0, regime=MigrationRegime.EMERGENCY,
        policy_decision=_decision(), source_pool_id="pool-s",
        target_pool_id="pool-t", workload_estimate=_est(),
        absolute_deadline=datetime.utcnow() + timedelta(seconds=1))
    coord = _seed(_coord(sleeps), plan)
    calls = []

    def fail():
        calls.append(1)
        raise CodedError("CRIU_DUMP_TIMEOUT", "t/o", transience="TRANSIENT")

    with pytest.raises(DeadlineExceeded):
        coord._run_operation_with_retry(
            step_type="CHECKPOINT", operation_id="op-1", func=fail,
            timeout=30.0, default_code="CRIU_DUMP_TIMEOUT")
    assert len(calls) == 1 and sleeps == []  # no doomed sleep


# -- §10.8 rule 4: UNKNOWN is not an attempt --
def test_unknown_outcome_propagates_without_consuming_attempts():
    sleeps, calls = [], []

    def unknown():
        calls.append(1)
        from orchestrator.migration_coordinator import OperationUnknownError
        raise OperationUnknownError("lost the thread")

    coord = _seed(_coord(sleeps), _plan())
    with pytest.raises(OperationUnknownError):
        coord._run_operation_with_retry(
            step_type="TRANSFER", operation_id="op-9", func=unknown,
            timeout=30.0, default_code="TRANSFER_FAILED")
    assert len(calls) == 1 and sleeps == []


def test_timeout_resolves_succeeded_via_probe():
    sleeps = []
    coord = _coord(sleeps, step_timeout_seconds=0.05,
                   unknown_resolution_budget_seconds=5.0,
                   unknown_poll_interval_seconds=0.01)
    coord = _seed(coord, _plan())

    def slow():
        time.sleep(0.5)
        return "late"

    out = coord._run_operation_with_retry(
        step_type="TRANSFER", operation_id="op-1", func=slow,
        timeout=0.05, probe=lambda op: {"state": "SUCCEEDED", "result": "ok"},
        default_code="TRANSFER_FAILED")
    assert out == "ok"


def test_timeout_resolves_failed_via_probe_with_code():
    coord = _coord([], step_timeout_seconds=0.05,
                   unknown_resolution_budget_seconds=5.0,
                   unknown_poll_interval_seconds=0.01)
    coord = _seed(coord, _plan())

    def slow():
        time.sleep(0.5)
        return "late"

    with pytest.raises(CodedError) as ei:
        coord._run_operation_with_retry(
            step_type="TRANSFER", operation_id="op-1", func=slow,
            timeout=0.05,
            probe=lambda op: {"state": "FAILED", "failure_code": "TRANSFER_FAILED"},
            default_code="TRANSFER_FAILED")
    assert ei.value.failure_code == "TRANSFER_FAILED"


def test_timeout_unresolved_within_budget_escalates_unknown():
    coord = _coord([], step_timeout_seconds=0.05,
                   unknown_resolution_budget_seconds=0.05,
                   unknown_poll_interval_seconds=0.01)
    coord = _seed(coord, _plan())

    def slow():
        time.sleep(0.5)
        return "late"

    with pytest.raises(OperationUnknownError):
        coord._run_operation_with_retry(
            step_type="TRANSFER", operation_id="op-1", func=slow,
            timeout=0.05, probe=lambda op: {"state": "UNKNOWN"},
            default_code="TRANSFER_FAILED")


# -- §10.8 rule 5: priority never overrides criticality --
def test_schedule_order_best_effort_never_preempts_safety():
    steps = [
        SimpleNamespace(step_id="cleanup", priority=1,
                        criticality=Criticality.BEST_EFFORT),
        SimpleNamespace(step_id="fence", priority=100,
                        criticality=Criticality.SAFETY_CRITICAL),
        SimpleNamespace(step_id="provision", priority=30,
                        criticality=Criticality.EXECUTION_CRITICAL),
    ]
    assert [s.step_id for s in schedule_order(steps)] == [
        "provision", "fence", "cleanup"]


# -- §10.8 rule 6: injection points rejected outside harness --
def test_reject_injection_points_prod_vs_harness():
    steps = [{"step_id": "s-fence", "failure_injection_points": ["during_fencing"]}]
    with pytest.raises(ValueError, match="failure_injection_points"):
        reject_injection_points(steps, test_harness=False)
    reject_injection_points(steps, test_harness=True)  # no raise
    reject_injection_points([{"step_id": "s", "failure_injection_points": []}],
                            test_harness=False)


def test_execute_plan_rejects_injected_dag_before_side_effects(tmp_path):
    import json
    from storage.job_registry import JobRegistry
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({"job-1": {
        "job_id": "job-1", "state": "RUNNING", "region": "us-east-1",
        "public_ip": "10.0.0.9", "pid": 4242, "version": 0,
        "execution_epoch": 0, "active_migration_id": None}}))
    reg = JobRegistry(str(path))
    coord = MigrationCoordinator(
        registry=reg, provisioner=SimpleNamespace(),
        checkpoint_manager=SimpleNamespace(), transfer_manager=SimpleNamespace(),
        validator=SimpleNamespace(), cleanup_executor=SimpleNamespace())
    plan = _plan()
    dag = MigrationPlanner().to_v2_dag(plan, injection_points={"FENCE": ["during_fencing"]})
    out = coord.execute_plan(plan, dag_steps=dag)
    assert out.current_state == MigrationState.FAILED
    assert reg.get("job-1")["state"] == "RUNNING"  # never entered MIGRATING


# -- DAG carries §10.8 directives --
def test_dag_steps_carry_retry_codes_priorities_and_empty_injection():
    dag = MigrationPlanner().to_v2_dag(_plan())
    by_type = {s.type.name: s for s in dag}
    assert by_type["FENCE"].priority < by_type["CHECKPOINT"].priority
    assert by_type["CHECKPOINT"].retryable_failure_codes  # non-empty
    assert by_type["FENCE"].retryable_failure_codes == []
    assert by_type["VALIDATE"].retryable_failure_codes == []
    assert all(s.failure_injection_points == [] for s in dag)


# -- plan-creation dedup + replan/supersession (S15/S16) --
def test_plan_creation_dedup_returns_issued_plan():
    planner = MigrationPlanner()
    kwargs = dict(job_id="job-1", execution_epoch=0,
                  regime=MigrationRegime.ARBITRAGE, policy_decision=_decision(),
                  source_pool_id="pool-s", target_pool_id="pool-t",
                  workload_estimate=_est(), migration_id="mig-dedup")
    p1 = planner.create_plan(**kwargs)
    p2 = planner.create_plan(**kwargs)
    assert p2 is p1  # duplicate request → same plan, no double issuance


def test_successor_plan_links_supersedes_and_keeps_attempt():
    planner = MigrationPlanner()
    old = _plan_dd(planner)
    new = planner.create_successor_plan(old, reason="deadline-drift",
                                        workload_estimate=_est())
    assert new.plan_id != old.plan_id
    assert new.migration_id == old.migration_id  # same attempt, new plan
    assert new.supersedes_plan_id == old.plan_id
    assert "replan:deadline-drift" in new.authorization_reference
    # Repeating the same replan request dedups instead of forking again.
    assert planner.create_successor_plan(
        old, reason="deadline-drift", workload_estimate=_est()) is new


def _plan_dd(planner):
    return planner.create_plan(
        job_id="job-1", execution_epoch=0, regime=MigrationRegime.ARBITRAGE,
        policy_decision=_decision(), source_pool_id="pool-s",
        target_pool_id="pool-t", workload_estimate=_est(),
        migration_id="mig-successor")


def test_supersede_and_replan_marks_superseded_and_returns_successor():
    planner = MigrationPlanner()
    old = _plan_dd(planner)
    cleaned = []

    class _Cleanup:
        def cleanup_migration(self, plan, operations, safety_critical_only=False):
            cleaned.append(safety_critical_only)

    coord = MigrationCoordinator(
        registry=SimpleNamespace(get=lambda j: None,
                                 transition=lambda *a, **k: None),
        provisioner=SimpleNamespace(), checkpoint_manager=SimpleNamespace(),
        transfer_manager=SimpleNamespace(), validator=SimpleNamespace(),
        cleanup_executor=_Cleanup())
    coord = _seed(coord, old)
    new = coord.supersede_and_replan(planner, reason="emergency-preempts",
                                     workload_estimate=_est())
    assert new.supersedes_plan_id == old.plan_id
    assert coord._execution_state.current_state == MigrationState.SUPERSEDED
    assert cleaned == [True]  # safety-critical cleanup only (§6.7)


# -- end-to-end: op-ID reuse across retries inside a real phase --
def test_checkpoint_phase_retries_reuse_operation_id(tmp_path):
    import json
    from storage.job_registry import JobRegistry
    from orchestrator.checkpoint_manager import CheckpointManager
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({"job-1": {
        "job_id": "job-1", "state": "RUNNING", "region": "us-east-1",
        "public_ip": "10.0.0.9", "pid": 4242, "version": 0,
        "execution_epoch": 0, "active_migration_id": None}}))
    reg = JobRegistry(str(path))
    calls, sleeps = [], []

    def flaky_dump(job_id, pid, host, timeout=300.0):
        calls.append(1)
        raise CodedError("CRIU_DUMP_TIMEOUT", "dump timed out",
                         transience="TRANSIENT")

    coord = MigrationCoordinator(
        registry=reg, provisioner=SimpleNamespace(),
        checkpoint_manager=CheckpointManager(
            storage=SimpleNamespace(upload=lambda *a, **k: None,
                                    download=lambda *a, **k: None,
                                    bucket="bkt"),
            dump_handler=flaky_dump),
        transfer_manager=SimpleNamespace(), validator=SimpleNamespace(),
        cleanup_executor=SimpleNamespace(
            cleanup_migration=lambda *a, **k: None),
        step_timeout_seconds=30.0, sleep_fn=sleeps.append)
    out = coord.execute_plan(_plan())
    assert out.current_state == MigrationState.FAILED
    assert len(calls) == 3  # budget honored inside the live phase
    ops = [o for o in out.operations if o.step_name == "checkpointing"]
    assert len(ops) == 1  # same operation_id reused (I11), not re-minted
    assert ops[0].retries == 2
