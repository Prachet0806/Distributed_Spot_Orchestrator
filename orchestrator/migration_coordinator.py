from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional, Callable, Any
import concurrent.futures
import logging
import time

from orchestrator.migration_planner import MigrationPlan, MigrationState
from orchestrator.recovery_feasibility import RecoveryFeasibility
from orchestrator.policy_engine import MigrationRegime
from orchestrator.step_retry import (
    CodedError,
    DEFAULT_UNKNOWN_RESOLUTION_BUDGET_SECONDS,
    backoff_delay_seconds,
    failure_code_of,
    reject_injection_points,
    should_retry,
    spec_for,
)

logger = logging.getLogger(__name__)


class CoordinatorState(str, Enum):
    IDLE = "IDLE"
    EXECUTING = "EXECUTING"
    ABORTING = "ABORTING"
    RECOVERING = "RECOVERING"
    RECONCILING = "RECONCILING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass
class OperationRecord:
    operation_id: str
    step_name: str
    started_at: datetime
    completed_at: Optional[datetime] = None
    status: str = "RUNNING"  # RUNNING | SUCCEEDED | FAILED | UNKNOWN
    result: Any = None
    error: Optional[str] = None
    retries: int = 0


@dataclass
class MigrationExecutionState:
    plan: MigrationPlan
    current_state: MigrationState = MigrationState.PLANNED
    state_entered_at: datetime = field(default_factory=datetime.utcnow)
    operations: list[OperationRecord] = field(default_factory=list)
    source_ownership: bool = True
    target_ownership: bool = False
    fencing_confirmed: bool = False
    execution_epoch: int = 0
    expected_epoch: int = 0  # registry epoch we expect (rotates at fencing)
    deadline_exceeded: bool = False
    abort_requested: bool = False
    superseded_by: Optional[str] = None
    target_instance_id: Optional[str] = None
    checkpoint_id: Optional[str] = None
    provisioned: bool = False
    pool_slot_held: bool = False  # Track C3: per-pool concurrency slot
    source_snapshot: Optional[dict] = None


# MigrationState -> PlanStepType name for plan_store tracking.
_STATE_STEP_TYPES = {
    MigrationState.PRECHECKING: "PRECHECK",
    MigrationState.CHECKPOINTING: "CHECKPOINT",
    MigrationState.PERSISTING: "PERSIST",
    MigrationState.PROVISIONING: "PROVISION",
    MigrationState.TRANSFERRING: "TRANSFER",
    MigrationState.RESTORING: "RESTORE",
    MigrationState.FENCING: "FENCE",
    MigrationState.VALIDATING: "VALIDATE",
    MigrationState.ACTIVATING: "ACTIVATE",
    MigrationState.FINALIZING: "FINALIZE",
}

# Post-fencing states: deadline expiry never interrupts, forward recovery only.
_POST_FENCE_STATES = frozenset({
    MigrationState.FENCING, MigrationState.VALIDATING,
    MigrationState.ACTIVATING, MigrationState.FINALIZING,
})


class MigrationCoordinator:
    """Executes an approved plan exactly (Protocols #3, #4, #8).

    The Coordinator owns step ordering, operation IDs (ULID, sole minter),
    remaining-budget arithmetic (monotonic clock), and abort/retry/cleanup.
    It never invents objectives, never re-plans, and never confirms fencing
    without both epoch invalidation and verified source termination.
    """

    def __init__(
        self,
        registry: Any,
        provisioner: Any,
        checkpoint_manager: Any,
        transfer_manager: Any,
        validator: Any,
        cleanup_executor: Any,
        max_step_retries: int = 3,
        step_timeout_seconds: float = 300.0,
        fencing_timeout_seconds: float = 60.0,
        plan_store: Any = None,
        checkpoint_store: Any = None,
        monotonic_clock: Optional[Callable[[], float]] = None,
        fence_invalidate: Optional[Callable[..., Any]] = None,
        fence_terminate: Optional[Callable[..., Any]] = None,
        fence_verify: Optional[Callable[..., Any]] = None,
        readiness_hook: Optional[Callable[..., Any]] = None,
        snapshot_provider: Optional[Callable[[str, str], Optional[dict]]] = None,
        # §10.8 execution directives (Track C5). `retry_baseline` is the
        # `retry:` block of config/v2_baseline.yaml; `max_step_retries`
        # remains as a legacy per-coordinator attempt cap.
        retry_baseline: Optional[dict] = None,
        unknown_resolution_budget_seconds: float = DEFAULT_UNKNOWN_RESOLUTION_BUDGET_SECONDS,
        unknown_poll_interval_seconds: float = 1.0,
        test_harness: bool = False,
        sleep_fn: Optional[Callable[[float], None]] = None,
        # Track C3 double-guard: PoolConcurrencyTracker. Placement peeks at
        # admission; the Coordinator acquires before provisioning and
        # releases at every terminal outcome. None disables the guard.
        pool_concurrency: Optional[Any] = None,
        # Track B1/I14: append-only AuditStore. FENCE_STARTED is
        # audit-before-irreversible: a failed write blocks fencing.
        audit_store: Optional[Any] = None,
    ):
        self.registry = registry
        self.provisioner = provisioner
        self.checkpoint_manager = checkpoint_manager
        self.transfer_manager = transfer_manager
        self.validator = validator
        self.cleanup_executor = cleanup_executor
        self.max_step_retries = max_step_retries
        self.step_timeout_seconds = step_timeout_seconds
        self.fencing_timeout_seconds = fencing_timeout_seconds
        self.plan_store = plan_store
        self.checkpoint_store = checkpoint_store
        self._retry_baseline = retry_baseline or {}
        self._unknown_budget = unknown_resolution_budget_seconds
        self._unknown_poll = unknown_poll_interval_seconds
        self._test_harness = test_harness
        self._sleep = sleep_fn or time.sleep
        self._pool_concurrency = pool_concurrency
        self._audit_store = audit_store
        self._monotonic = monotonic_clock or time.monotonic
        self._fence_invalidate = fence_invalidate
        self._fence_terminate = fence_terminate
        self._fence_verify = fence_verify
        self._readiness_hook = readiness_hook
        self._snapshot_provider = snapshot_provider

        self._execution_state: Optional[MigrationExecutionState] = None
        self._state_machine = self._build_state_machine()
        self._deadline_monitor_active = False
        self._start_mono = 0.0

    def _build_state_machine(self) -> dict:
        return {
            MigrationState.PLANNED: {"next": MigrationState.PRECHECKING},
            MigrationState.PRECHECKING: {"next": MigrationState.CHECKPOINTING, "abort": MigrationState.ABORTING},
            MigrationState.CHECKPOINTING: {"next": MigrationState.PERSISTING, "abort": MigrationState.ABORTING, "retry": MigrationState.CHECKPOINTING},
            MigrationState.PERSISTING: {"next": MigrationState.PROVISIONING, "abort": MigrationState.ABORTING, "retry": MigrationState.PERSISTING},
            MigrationState.PROVISIONING: {"next": MigrationState.TRANSFERRING, "abort": MigrationState.ABORTING, "retry": MigrationState.PROVISIONING},
            MigrationState.TRANSFERRING: {"next": MigrationState.RESTORING, "abort": MigrationState.ABORTING, "retry": MigrationState.TRANSFERRING},
            MigrationState.RESTORING: {"next": MigrationState.FENCING, "abort": MigrationState.ABORTING, "retry": MigrationState.RESTORING},
            # No abort out of FENCING: an in-progress ownership transition
            # always completes (COMPLETE_FENCING, Protocols #4 §6.6).
            MigrationState.FENCING: {"next": MigrationState.VALIDATING, "failed": MigrationState.FAILED},
            MigrationState.VALIDATING: {"next": MigrationState.ACTIVATING, "abort": MigrationState.ABORTING, "retry": MigrationState.RESTORING, "failed": MigrationState.FAILED},
            MigrationState.ACTIVATING: {"next": MigrationState.FINALIZING, "failed": MigrationState.FAILED},
            MigrationState.FINALIZING: {"next": MigrationState.SUCCESS, "failed": MigrationState.FAILED},
            MigrationState.ABORTING: {"next": MigrationState.ABORTED, "cleanup_failed": MigrationState.FAILED},
            MigrationState.SUCCESS: {},
            MigrationState.ABORTED: {},
            MigrationState.SUPERSEDED: {},
            MigrationState.FAILED: {},
        }

    # -- public entry --
    def execute_plan(
        self,
        plan: MigrationPlan,
        on_state_change: Optional[Callable[[MigrationState, MigrationState], None]] = None,
        dag_steps: Optional[list] = None,
    ) -> MigrationExecutionState:
        # §10.8 rule 6: production plans must not carry fault-injection
        # hooks. Rejected before any state transition or side effect.
        try:
            reject_injection_points(dag_steps or [], test_harness=self._test_harness)
        except ValueError as e:
            # Rejected before any transition or side effect: the job never
            # entered MIGRATING, so no registry write is owed.
            self._execution_state = MigrationExecutionState(
                plan=plan,
                current_state=MigrationState.FAILED,
                state_entered_at=datetime.utcnow(),
                execution_epoch=plan.execution_epoch,
                expected_epoch=plan.execution_epoch,
                checkpoint_id=plan.checkpoint_id,
            )
            logger.error("Plan %s rejected: %s", plan.migration_id, e)
            return self._execution_state
        self._execution_state = MigrationExecutionState(
            plan=plan,
            current_state=MigrationState.PLANNED,
            state_entered_at=datetime.utcnow(),
            execution_epoch=plan.execution_epoch,
            expected_epoch=plan.execution_epoch,
            checkpoint_id=plan.checkpoint_id,
        )
        self._start_mono = self._monotonic()

        logger.info(f"Starting migration {plan.migration_id} for job {plan.job_id} (regime: {plan.regime.value})")

        if plan.is_expired():
            logger.warning(f"Plan {plan.migration_id} expired before execution")
            self._transition_to(MigrationState.FAILED, "Plan expired")
            self._job_transition_safe(plan, "FAILED")
            return self._execution_state

        try:
            self._job_transition_safe(plan, "MIGRATING",
                                      active_migration_id=plan.migration_id)
            self._transition_to(MigrationState.PRECHECKING, on_state_change)
            self._execute_prechecking()

            if plan.regime == MigrationRegime.EMERGENCY:
                self._execute_emergency_preparation(plan, on_state_change)
            else:
                self._transition_to(MigrationState.CHECKPOINTING, on_state_change)
                self._execute_checkpointing()

                self._transition_to(MigrationState.PERSISTING, on_state_change)
                self._execute_persisting()

                self._transition_to(MigrationState.PROVISIONING, on_state_change)
                self._execute_provisioning()

            self._final_gate(plan, "TRANSFER")
            self._transition_to(MigrationState.TRANSFERRING, on_state_change)
            self._execute_transferring()

            self._final_gate(plan, "RESTORE")
            self._transition_to(MigrationState.RESTORING, on_state_change)
            self._execute_restoring()

            self._final_gate(plan, "FENCE")
            self._transition_to(MigrationState.FENCING, on_state_change)
            self._execute_fencing()

            self._transition_to(MigrationState.VALIDATING, on_state_change)
            self._execute_validating()

            self._final_gate(plan, "ACTIVATE")
            self._transition_to(MigrationState.ACTIVATING, on_state_change)
            self._execute_activating()

            self._transition_to(MigrationState.FINALIZING, on_state_change)
            self._execute_finalizing()

            self._transition_to(MigrationState.SUCCESS, on_state_change)
            self._release_pool_slot()
            try:
                self._audit("MIGRATION_COMPLETED", outcome="SUCCESS")
            except Exception as exc:
                logger.error("MIGRATION_COMPLETED audit write failed: %s", exc)
            logger.info(f"Migration {plan.migration_id} completed successfully")

        except MigrationAborted as e:
            logger.warning(f"Migration {plan.migration_id} aborted: {e}")
            self._handle_abort(str(e))
        except MigrationSuperseded as e:
            logger.warning(f"Migration {plan.migration_id} superseded: {e}")
            self._handle_superseded(str(e))
        except MigrationFailed as e:
            logger.error(f"Migration {plan.migration_id} failed: {e}")
            self._handle_failure(str(e))
        except DeadlineExceeded as e:
            logger.error(f"Migration {plan.migration_id} deadline exceeded: {e}")
            self._handle_deadline_exceeded(str(e))
        except Exception as e:
            logger.exception(f"Migration {plan.migration_id} unexpected error: {e}")
            self._handle_failure(f"Unexpected error: {e}")

        return self._execution_state

    # -- phases --
    def _execute_prechecking(self):
        state = self._execution_state
        plan = state.plan

        self._check_plan_validity(plan)
        self._verify_candidate_readiness(plan.target_pool_id)
        self._verify_ownership(plan.job_id, plan.execution_epoch)
        self._check_deadline_budget(plan)

    def _execute_emergency_preparation(self, plan, on_state_change):
        """CHECKPOINT→PERSIST ∥ PROVISION, join ALL_SUCCEEDED at TRANSFER."""
        state = self._execution_state
        self._transition_to(MigrationState.CHECKPOINTING, on_state_change)

        branch_errors: dict[str, Exception] = {}

        def _checkpoint_branch():
            try:
                self._execute_checkpointing()
                self._transition_to(MigrationState.PERSISTING, on_state_change)
                self._execute_persisting()
            except Exception as e:  # noqa: BLE001 — collected for join
                branch_errors["checkpoint"] = e

        def _provision_branch():
            try:
                self._transition_to(MigrationState.PROVISIONING, on_state_change)
                self._execute_provisioning()
            except Exception as e:  # noqa: BLE001 — collected for join
                branch_errors["provision"] = e

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futs = [pool.submit(_checkpoint_branch), pool.submit(_provision_branch)]
            concurrent.futures.wait(futs)

        if branch_errors:
            # ALL_SUCCEEDED join: clean the surviving branch, then fail.
            try:
                if state.provisioned and "checkpoint" in branch_errors:
                    self._terminate_target_best_effort()
            finally:
                first = next(iter(branch_errors.values()))
                raise MigrationFailed(f"Emergency preparation branch failed: {first}")

    def _execute_checkpointing(self):
        state = self._execution_state
        plan = state.plan
        op_id = self._start_operation("checkpointing")
        try:
            job = self._get_job(plan.job_id)
            pid = (job or {}).get("pid", 0)
            host = (job or {}).get("public_ip")
            dump = getattr(self.checkpoint_manager, "dump", None)
            probe = getattr(self.checkpoint_manager, "get_operation_status", None)
            if callable(dump):
                result = self._run_operation_with_retry(
                    step_type="CHECKPOINT", operation_id=op_id,
                    func=lambda: dump(job_id=plan.job_id, pid=pid, host=host),
                    timeout=self.step_timeout_seconds, probe=probe,
                    default_code="CRIU_DUMP_FAILED",
                )
            else:
                result = self._run_operation_with_retry(
                    step_type="CHECKPOINT", operation_id=op_id,
                    func=lambda: self.checkpoint_manager.create_checkpoint(plan=plan),
                    timeout=self.step_timeout_seconds, probe=probe,
                    default_code="CRIU_DUMP_FAILED",
                )
            checkpoint_id = getattr(result, "checkpoint_id", plan.migration_id)
            state.checkpoint_id = checkpoint_id
            state.source_snapshot = self._take_snapshot(plan.job_id, "pre")
            self._complete_operation(op_id, result)
        except OperationUnknownError as e:
            self._fail_operation(op_id, str(e), unknown=True)
            raise MigrationFailed(f"Checkpointing outcome unknown: {e}")
        except Exception as e:
            self._fail_operation(op_id, str(e))
            raise MigrationFailed(f"Checkpointing failed: {e}")

    def _execute_persisting(self):
        state = self._execution_state
        plan = state.plan
        op_id = self._start_operation("persisting")
        try:
            checkpoint_id = state.checkpoint_id or plan.migration_id
            persist = getattr(self.checkpoint_manager, "persist", None)
            probe = getattr(self.checkpoint_manager, "get_operation_status", None)
            if callable(persist):
                result = self._run_operation_with_retry(
                    step_type="PERSIST", operation_id=op_id,
                    func=lambda: persist(checkpoint_id, job_id=plan.job_id),
                    timeout=self.step_timeout_seconds, probe=probe,
                    default_code="CHECKPOINT_PERSIST_FAILED",
                )
            else:
                result = self._run_operation_with_retry(
                    step_type="PERSIST", operation_id=op_id,
                    func=lambda: self.checkpoint_manager.persist_checkpoint(plan.migration_id),
                    timeout=self.step_timeout_seconds, probe=probe,
                    default_code="CHECKPOINT_PERSIST_FAILED",
                )
            self._complete_operation(op_id, result)
        except OperationUnknownError as e:
            self._fail_operation(op_id, str(e), unknown=True)
            raise MigrationFailed(f"Persistence outcome unknown: {e}")
        except Exception as e:
            self._fail_operation(op_id, str(e))
            raise MigrationFailed(f"Persistence failed: {e}")

    def _acquire_pool_slot(self):
        """Execution-time concurrency guard (second of the double-guard)."""
        state = self._execution_state
        if self._pool_concurrency is None or state.pool_slot_held:
            return
        if not self._pool_concurrency.admit(state.plan.target_pool_id):
            raise MigrationFailed(
                f"Pool {state.plan.target_pool_id} at concurrency limit "
                f"(CAPACITY_UNAVAILABLE)")
        state.pool_slot_held = True

    def _release_pool_slot(self):
        state = self._execution_state
        if state is not None and state.pool_slot_held:
            state.pool_slot_held = False
            try:
                if self._pool_concurrency is not None:
                    self._pool_concurrency.release(state.plan.target_pool_id)
            except Exception as exc:
                logger.warning("Pool slot release failed: %s", exc)

    def _execute_provisioning(self):
        state = self._execution_state
        plan = state.plan
        if state.provisioned:
            return  # already provisioned by the emergency parallel branch
        self._acquire_pool_slot()
        op_id = self._start_operation("provisioning")
        try:
            provision = getattr(self.provisioner, "provision_with_operation", None)
            tags = {"job_id": plan.job_id, "migration_id": plan.migration_id,
                    "execution_epoch": str(plan.execution_epoch)}
            probe = getattr(self.provisioner, "get_operation_status", None)
            if callable(provision):
                result = self._run_operation_with_retry(
                    step_type="PROVISION", operation_id=op_id,
                    func=lambda: provision(plan.target_pool_id, operation_id=op_id,
                                           tags=tags),
                    timeout=self.step_timeout_seconds, probe=probe,
                    default_code="PROVISION_FAILED",
                )
            else:
                result = self._run_operation_with_retry(
                    step_type="PROVISION", operation_id=op_id,
                    func=lambda: self.provisioner.provision(plan.target_pool_id),
                    timeout=self.step_timeout_seconds, probe=probe,
                    default_code="PROVISION_FAILED",
                )
            state.target_instance_id = getattr(result, "instance_id", None)
            state.provisioned = True
            self._complete_operation(op_id, result)
        except OperationUnknownError as e:
            self._fail_operation(op_id, str(e), unknown=True)
            raise MigrationFailed(f"Provisioning outcome unknown: {e}")
        except Exception as e:
            self._fail_operation(op_id, str(e))
            raise MigrationFailed(f"Provisioning failed: {e}")

    def _execute_transferring(self):
        state = self._execution_state
        plan = state.plan
        op_id = self._start_operation("transferring")
        try:
            transfer = self.transfer_manager
            probe = getattr(transfer, "get_operation_status", None)

            def _transfer_call():
                if hasattr(transfer, "download"):
                    try:
                        return transfer.download(plan.job_id)
                    except TypeError:
                        return transfer.transfer(plan.migration_id)
                return transfer.transfer(plan.migration_id)

            result = self._run_operation_with_retry(
                step_type="TRANSFER", operation_id=op_id,
                func=_transfer_call,
                timeout=self.step_timeout_seconds, probe=probe,
                default_code="TRANSFER_FAILED",
            )
            self._complete_operation(op_id, result)
        except OperationUnknownError as e:
            self._fail_operation(op_id, str(e), unknown=True)
            raise MigrationFailed(f"Transfer outcome unknown: {e}")
        except Exception as e:
            self._fail_operation(op_id, str(e))
            raise MigrationFailed(f"Transfer failed: {e}")

    def _execute_restoring(self):
        state = self._execution_state
        plan = state.plan
        op_id = self._start_operation("restoring")
        try:
            checkpoint_id = state.checkpoint_id or plan.migration_id
            restore = getattr(self.checkpoint_manager, "restore", None)
            probe = getattr(self.checkpoint_manager, "get_operation_status", None)
            if callable(restore):
                result = self._run_operation_with_retry(
                    step_type="RESTORE", operation_id=op_id,
                    func=lambda: restore(checkpoint_id, host=state.target_instance_id),
                    timeout=self.step_timeout_seconds, probe=probe,
                    default_code="CRIU_RESTORE_FAILED",
                )
            else:
                result = self._run_operation_with_retry(
                    step_type="RESTORE", operation_id=op_id,
                    func=lambda: self.checkpoint_manager.restore_checkpoint(plan.migration_id),
                    timeout=self.step_timeout_seconds, probe=probe,
                    default_code="CRIU_RESTORE_FAILED",
                )
            outcome = getattr(result, "outcome", None) or (
                "SUCCEEDED" if getattr(result, "success", True) else "FAILED")
            if outcome == "PARTIAL_RESTORE":
                self._complete_operation(op_id, result)
                raise MigrationFailed("Partial restore: target contained, checkpoint intact")
            if outcome == "UNKNOWN":
                raise OperationUnknownError("restore reported UNKNOWN")
            if outcome != "SUCCEEDED":
                raise MigrationFailed(f"Restore reported {outcome}")
            self._complete_operation(op_id, result)
        except OperationUnknownError as e:
            self._fail_operation(op_id, str(e), unknown=True)
            raise MigrationFailed(f"Restore outcome unknown: {e}")
        except MigrationFailed:
            raise
        except Exception as e:
            self._fail_operation(op_id, str(e))
            if state.fencing_confirmed:
                raise MigrationFailed(f"Restore failed post-fencing: {e}")
            raise MigrationFailed(f"Restore failed: {e}")

    def _audit(self, event_type: str, **fields) -> Optional[dict]:
        """Append a migration-scoped audit record (correlation envelope).

        Returns the record, or None when no store is wired. Callers decide
        gating: pre-irreversible events fail closed, post-facto events log.
        """
        store = self._audit_store
        if store is None:
            return None
        append = getattr(store, "append", None)
        if not callable(append):
            return None
        from orchestrator.models_v2 import new_ulid_like
        state = self._execution_state
        plan = state.plan if state is not None else None
        record = {
            "audit_id": new_ulid_like(),
            "event_type": event_type,
            "timestamp": datetime.utcnow().isoformat(),
            "component": "MigrationCoordinator",
        }
        if plan is not None:
            record.update({
                "job_id": plan.job_id,
                "migration_id": plan.migration_id,
                "execution_epoch": (state.expected_epoch
                                    if state is not None
                                    else plan.execution_epoch),
                "correlation_id": plan.migration_id,
                "plan_id": getattr(plan, "plan_id", ""),
                "plan_hash": getattr(plan, "plan_hash", ""),
            })
        record.update(fields)
        return append(record)

    def _execute_fencing(self):
        state = self._execution_state
        plan = state.plan

        if not plan.fencing_authorized:
            raise MigrationFailed("Fencing not authorized in plan")
        # I14 audit-before-irreversible: FENCE_STARTED must be durably
        # recorded before any fence hook runs; a failed write blocks fencing.
        try:
            self._audit("FENCE_STARTED")
        except Exception as e:
            raise MigrationFailed(f"audit-before-fence failed: {e}")
        op_id = self._start_operation("fencing")

        try:
            self._run_with_timeout(
                lambda: self._fence_invalidate_hook(plan),
                timeout=self.fencing_timeout_seconds, op_id=op_id + "-invalidate",
            )
            self._run_with_timeout(
                lambda: self._fence_terminate_hook(plan),
                timeout=self.fencing_timeout_seconds, op_id=op_id + "-terminate",
            )
            self._run_with_timeout(
                lambda: self._fence_verify_hook(plan),
                timeout=self.fencing_timeout_seconds, op_id=op_id + "-verify",
            )
            # Confirmed fencing = epoch invalidation + verified termination.
            # Rotate the epoch now so the fenced source can never resume as
            # authoritative, even if validation fails afterwards.
            self._job_transition_safe(
                plan, "MIGRATING", ownership_change=True,
                expected_epoch=state.expected_epoch,
                active_migration_id=plan.migration_id,
            )
            state.expected_epoch += 1
            self._complete_operation(op_id, {"fenced": True})
            state.fencing_confirmed = True
            state.source_ownership = False
            state.target_ownership = True
            try:
                self._audit("FENCE_CONFIRMED", execution_epoch=state.expected_epoch)
            except Exception as exc:
                logger.error("FENCE_CONFIRMED audit write failed: %s", exc)
        except Exception as e:
            self._fail_operation(op_id, str(e))
            raise MigrationFailed(f"Fencing failed: {e}")

    def _execute_validating(self):
        state = self._execution_state
        plan = state.plan
        op_id = self._start_operation("validating")
        try:
            target_snapshot = self._take_snapshot(plan.job_id, "post")
            result = self._run_with_timeout(
                lambda: self.validator.validate(
                    plan.migration_id, plan.job_id, plan.execution_epoch + 1,
                    source_snapshot=state.source_snapshot,
                    target_snapshot=target_snapshot),
                timeout=self.step_timeout_seconds, op_id=op_id,
            )
            self._complete_operation(op_id, result)
            if not self._validation_passed(result):
                if state.fencing_confirmed:
                    raise MigrationFailed("Validation failed post-fencing: forward recovery only")
                raise MigrationFailed("Validation failed")
        except MigrationFailed:
            raise
        except OperationUnknownError as e:
            self._fail_operation(op_id, str(e), unknown=True)
            raise MigrationFailed(f"Validation outcome unknown: {e}")
        except Exception as e:
            self._fail_operation(op_id, str(e))
            if state.fencing_confirmed:
                raise MigrationFailed(f"Validation error post-fencing: {e}")
            raise MigrationFailed(f"Validation error: {e}")

    @staticmethod
    def _validation_passed(result: Any) -> bool:
        if isinstance(result, dict):
            if "passed" in result:
                return bool(result["passed"])
            overall = str(result.get("overall_result", result.get("result", ""))).upper()
            return overall in ("PASSED", "VALID", "SUCCEEDED")
        overall = getattr(result, "overall_result", None) or getattr(result, "result", None)
        if overall is not None:
            return str(getattr(overall, "value", overall)).upper() in ("PASSED", "VALID", "SUCCEEDED")
        if hasattr(result, "success"):
            return bool(result.success)
        return False

    def _execute_activating(self):
        state = self._execution_state
        plan = state.plan
        op_id = self._start_operation("activating")
        try:
            # Ownership was already rotated at fencing; activation only moves
            # the lifecycle to RUNNING and clears the active migration.
            self._job_transition_safe(
                plan, "RUNNING",
                expected_epoch=state.expected_epoch,
                clear_active_migration=True,
                target_pool_id=plan.target_pool_id,
                target_instance_id=state.target_instance_id,
            )
            self._complete_operation(op_id, {"activated": True})
        except Exception as e:
            self._fail_operation(op_id, str(e))
            raise MigrationFailed(f"Activation failed: {e}")

    def _execute_finalizing(self):
        state = self._execution_state
        plan = state.plan
        op_id = self._start_operation("finalizing")
        try:
            cleanup = getattr(self.cleanup_executor, "cleanup_migration", None)
            if callable(cleanup):
                try:
                    cleanup(plan, state.operations)
                except TypeError:
                    cleanup(plan)
            self._complete_operation(op_id, {"finalized": True})
        except Exception as e:
            self._fail_operation(op_id, str(e))
            raise MigrationFailed(f"Finalization failed: {e}")

    # -- transitions / gates --
    def _transition_to(self, new_state: MigrationState, callback=None):
        if self._execution_state is None:
            return
        old_state = self._execution_state.current_state
        if isinstance(callback, str):
            callback = None
        valid = (
            self._state_machine.get(old_state, {}).get("next") == new_state
            or self._state_machine.get(old_state, {}).get("retry") == new_state
            or self._state_machine.get(old_state, {}).get("abort") == new_state
            or self._state_machine.get(old_state, {}).get("failed") == new_state
        )
        if not valid and not (old_state == MigrationState.PLANNED and new_state == MigrationState.PRECHECKING):
            logger.warning(f"Transition {old_state} -> {new_state} may not be standard")

        self._execution_state.current_state = new_state
        self._execution_state.state_entered_at = datetime.utcnow()
        logger.info(f"Migration {self._execution_state.plan.migration_id}: {old_state} -> {new_state}")
        self._track_step_transition(old_state, new_state)

        if callable(callback):
            callback(old_state, new_state)

    def _track_step_transition(self, old: MigrationState, new: MigrationState):
        if self.plan_store is None or self._execution_state is None:
            return
        plan = self._execution_state.plan
        plan_id = getattr(plan, "plan_id", "") or plan.migration_id
        try:
            doc = self.plan_store.get_plan(plan_id)
        except Exception:
            return
        by_type = {s.get("type", "").upper(): s for s in doc.get("steps", [])}
        old_type = _STATE_STEP_TYPES.get(old)
        new_type = _STATE_STEP_TYPES.get(new)
        try:
            if old_type and old_type in by_type:
                cur = by_type[old_type].get("state", "PENDING")
                if cur in ("RUNNING", "PENDING"):
                    self.plan_store.update_step_state(
                        plan_id, by_type[old_type]["step_id"], cur, "SUCCEEDED")
            if new_type and new_type in by_type:
                cur = self.plan_store.get_plan(plan_id)["steps"]
                cur = next(s for s in cur if s.get("type", "").upper() == new_type)
                if cur.get("state") == "PENDING":
                    self.plan_store.update_step_state(
                        plan_id, cur["step_id"], "PENDING", "RUNNING")
        except Exception as exc:  # tracking must not break execution
            logger.warning("Plan step tracking skipped: %s", exc)

    def _check_plan_validity(self, plan: MigrationPlan):
        if plan.is_expired():
            raise MigrationAborted("Plan expired")
        if plan.regime == MigrationRegime.EMERGENCY:
            self._check_deadline_budget(plan)

    def _elapsed_mono(self) -> float:
        return self._monotonic() - self._start_mono

    def _check_deadline_budget(self, plan: MigrationPlan):
        if plan.regime != MigrationRegime.EMERGENCY:
            return
        remaining = None
        if plan.absolute_deadline is not None:
            remaining = (plan.absolute_deadline - datetime.utcnow()).total_seconds()
        elif plan.deadline_seconds:
            remaining = plan.deadline_seconds - self._elapsed_mono()
        if remaining is None:
            return
        required = (plan.estimated_critical_path_seconds or 0) + plan.safety_margin_seconds
        if remaining < required:
            self._execution_state.deadline_exceeded = True
            if self._execution_state.current_state in _POST_FENCE_STATES:
                logger.warning("Deadline exceeded but in post-fencing phase; continuing")
                return
            raise DeadlineExceeded(
                f"Insufficient deadline budget: {remaining:.0f}s remaining, {required:.0f}s required")

    def _final_gate(self, plan: MigrationPlan, step: str):
        """Last check before an irreversible step: expiry + deadline + epoch."""
        if plan.is_expired():
            raise MigrationAborted(f"Plan expired before {step}")
        if plan.regime == MigrationRegime.EMERGENCY:
            self._check_deadline_budget(plan)
        if step in ("FENCE", "ACTIVATE") and self._execution_state is not None:
            job = self._get_job(plan.job_id)
            if job is not None:
                current_epoch = job.get("execution_epoch", 0) or 0
                want = self._execution_state.expected_epoch
                if current_epoch != want:
                    raise MigrationFailed(
                        f"Epoch drift before {step}: expected={want} "
                        f"registry={current_epoch}")

    # -- hooks (fail closed) --
    def _verify_candidate_readiness(self, candidate_id: str):
        if self._readiness_hook is not None:
            self._readiness_hook(candidate_id)
            return
        logger.debug("No readiness hook; planner-time READY assessment stands")

    def _verify_ownership(self, job_id: str, epoch: int):
        job = self._get_job(job_id)
        if job is None:
            return
        current = job.get("execution_epoch", 0) or 0
        if current != epoch:
            raise MigrationFailed(
                f"Ownership mismatch for {job_id}: plan epoch {epoch}, registry {current}")

    def _fence_invalidate_hook(self, plan: MigrationPlan):
        if self._fence_invalidate is not None:
            self._fence_invalidate(plan)
            return
        raise MigrationFailed(
            "No fence-invalidate hook configured: refusing to confirm fencing "
            "from epoch invalidation alone")

    def _fence_terminate_hook(self, plan: MigrationPlan):
        if self._fence_terminate is not None:
            self._fence_terminate(plan)
            return
        raise MigrationFailed(
            "No fence-terminate hook configured: source termination unverified")

    def _fence_verify_hook(self, plan: MigrationPlan):
        if self._fence_verify is not None:
            result = self._fence_verify(plan)
            if result is False:
                raise MigrationFailed("Source still alive after fence")
            return
        raise MigrationFailed(
            "No fence-verify hook configured: fence confirmation refused")

    def _take_snapshot(self, job_id: str, phase: str) -> Optional[dict]:
        if self._snapshot_provider is None:
            return None
        try:
            return self._snapshot_provider(job_id, phase)
        except Exception as exc:
            logger.warning("Snapshot provider (%s) failed: %s", phase, exc)
            return None

    def _terminate_target_best_effort(self):
        state = self._execution_state
        if state and state.target_instance_id:
            try:
                self.provisioner.terminate(state.target_instance_id)
            except Exception as exc:
                logger.warning("Branch cleanup: target terminate failed: %s", exc)

    # -- operations --
    def _run_with_timeout(self, func: Callable, timeout: float, op_id: str = "",
                          probe: Optional[Callable] = None) -> Any:
        """Enforce step timeouts; ambiguous outcomes resolve or surface UNKNOWN.

        On timeout with a probe available, the outcome enters resolution
        (§10.8 rule 4): `GetOperationStatusQuery`-style polling within the
        UNKNOWN budget. Resolved SUCCEEDED returns the result; resolved
        FAILED raises it as a coded error; exhausted budget raises
        OperationUnknownError (never a blind retry).
        """
        eff = min(float(timeout or 0) or self.step_timeout_seconds,
                  self.step_timeout_seconds)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(func)
            try:
                return fut.result(timeout=eff)
            except concurrent.futures.TimeoutError as e:
                if probe is None:
                    raise OperationUnknownError(
                        f"operation {op_id or '?'} timed out after {eff:.0f}s") from e
                return self._resolve_unknown(probe, op_id, eff, e)

    @staticmethod
    def _probe_outcome(report: Any) -> tuple[str, Any, Optional[str]]:
        """Normalize a probe report → (SUCCEEDED|FAILED|UNKNOWN, payload, code)."""
        if isinstance(report, dict):
            raw = str(report.get("state", report.get("status", "UNKNOWN"))).upper()
            code = report.get("failure_code") or report.get("code")
            payload = report.get("result", report.get("payload", report))
        else:
            raw = str(getattr(report, "state",
                              getattr(report, "status",
                                      getattr(report, "outcome", "UNKNOWN")))).upper()
            if raw == "UNKNOWN" and getattr(report, "success", None) is True:
                raw = "SUCCEEDED"
            code = getattr(report, "failure_code", None) or getattr(report, "code", None)
            payload = getattr(report, "result", report)
        if raw in ("SUCCEEDED", "SUCCESS", "OK", "COMPLETED"):
            return "SUCCEEDED", payload, None
        if raw in ("FAILED", "FAILURE", "ERROR", "PARTIAL_RESTORE"):
            return "FAILED", payload, code or raw
        return "UNKNOWN", payload, None

    def _resolve_unknown(self, probe: Callable, op_id: str,
                         elapsed: float, cause: BaseException) -> Any:
        """Poll the executor/provider until the outcome resolves or escalates."""
        deadline = self._monotonic() + self._unknown_budget
        last_report: Any = None
        while self._monotonic() < deadline:
            try:
                try:
                    last_report = probe(op_id) if op_id else probe()
                except TypeError:
                    last_report = probe()
            except Exception as exc:  # probe errors never resolve; keep polling
                logger.debug("UNKNOWN probe for %s failed: %s", op_id or "?", exc)
                last_report = None
            outcome, payload, code = self._probe_outcome(last_report)
            if outcome == "SUCCEEDED":
                logger.info("operation %s UNKNOWN resolved: SUCCEEDED", op_id or "?")
                return payload
            if outcome == "FAILED":
                logger.warning("operation %s UNKNOWN resolved: FAILED (%s)",
                               op_id or "?", code)
                raise CodedError(code or "OPERATION_FAILED",
                                 f"operation {op_id or '?'} failed after timeout")
            self._sleep(self._unknown_poll)
        raise OperationUnknownError(
            f"operation {op_id or '?'} timed out after {elapsed:.0f}s; "
            f"unresolved after {self._unknown_budget:.0f}s → reconciliation") from cause

    def _retry_spec(self, step_type: str):
        spec = spec_for(step_type, baseline={"retry": {}} | dict(self._retry_baseline or {}))
        if self.max_step_retries != 3:
            # Legacy per-coordinator cap still honored as an upper bound.
            import dataclasses
            spec = dataclasses.replace(
                spec, max_attempts=min(spec.max_attempts, self.max_step_retries))
        return spec

    def _retry_budgets_valid(self, plan: MigrationPlan) -> bool:
        """§6.1: operation budget ∧ migration attempt budget ∧ deadline."""
        if plan.is_expired():
            return False
        return True

    def _backoff_or_deadline(self, plan: MigrationPlan, delay: float):
        """Sleep for backoff; backoff consumes the effective deadline (rule 3)."""
        if plan.regime == MigrationRegime.EMERGENCY and plan.absolute_deadline is not None:
            remaining = (plan.absolute_deadline - datetime.utcnow()).total_seconds()
            if delay >= remaining:
                raise DeadlineExceeded(
                    f"backoff {delay:.0f}s exceeds remaining deadline {remaining:.0f}s")
        self._sleep(delay)

    def _note_attempt(self, operation_id: str):
        for op in self._execution_state.operations:
            if op.operation_id == operation_id:
                op.retries += 1
                break

    def _run_operation_with_retry(
        self,
        *,
        step_type: str,
        operation_id: str,
        func: Callable[[], Any],
        timeout: float,
        probe: Optional[Callable] = None,
        default_code: str,
    ) -> Any:
        """Execute one step operation with §10.8 retry semantics.

        Every attempt reuses `operation_id` (I11). UNKNOWN outcomes never
        consume the attempt budget (rule 4). Unlisted failure codes escalate
        immediately (rule 2). Exhaustion re-raises the terminal error for
        the phase handler (abort/replan/forward-recovery per Protocol #4).
        """
        plan = self._execution_state.plan
        spec = self._retry_spec(step_type)
        attempts = 0
        while True:
            try:
                return self._run_with_timeout(
                    func, timeout=timeout, op_id=operation_id, probe=probe)
            except OperationUnknownError:
                raise  # not an attempt; caller routes to reconciliation path
            except Exception as exc:
                attempts += 1
                # Gate on the executor-reported code only: uncoded errors
                # have no listed code, so they escalate immediately (rule 2).
                # `default_code` names the failure for logs/audit.
                code = failure_code_of(exc, None)
                if not should_retry(spec, failure_code=code,
                                    attempts_used=attempts,
                                    budgets_valid=self._retry_budgets_valid(plan)):
                    if attempts > 1:
                        logger.info("step %s giving up after %d attempts (%s)",
                                    step_type, attempts, code or default_code)
                    raise
                self._note_attempt(operation_id)
                delay = backoff_delay_seconds(spec, attempts)
                logger.info("step %s attempt %d failed (%s); retrying in %.1fs "
                            "with operation %s", step_type, attempts, code,
                            delay, operation_id)
                self._backoff_or_deadline(plan, delay)

    def supersede_and_replan(self, planner: Any, reason: str, **overrides) -> Any:
        """Replan path (S15/S16): successor plan for the live execution.

        Marks the in-flight plan SUPERSEDED (safety-critical cleanup only —
        a superseded target is never reused, §6.7) and returns the linked
        successor. Executing the successor is the caller's decision (main
        loop / Recovery Policy → Planner → Coordinator).
        """
        state = self._execution_state
        if state is None or state.plan is None:
            raise MigrationFailed("no live plan to supersede")
        successor = planner.create_successor_plan(state.plan, reason, **overrides)
        self._handle_superseded(reason)
        logger.info("plan %s superseded by %s (%s)",
                    state.plan.plan_id, successor.plan_id, reason)
        return successor

    def _start_operation(self, step_name: str) -> str:
        from orchestrator.models_v2 import new_ulid_like
        op_id = new_ulid_like()
        self._execution_state.operations.append(OperationRecord(
            operation_id=op_id,
            step_name=step_name,
            started_at=datetime.utcnow(),
        ))
        return op_id

    def _complete_operation(self, operation_id: str, result: Any):
        for op in self._execution_state.operations:
            if op.operation_id == operation_id:
                op.completed_at = datetime.utcnow()
                op.status = "SUCCEEDED"
                op.result = result
                break

    def _fail_operation(self, operation_id: str, error: str, unknown: bool = False):
        for op in self._execution_state.operations:
            if op.operation_id == operation_id:
                op.completed_at = datetime.utcnow()
                op.status = "UNKNOWN" if unknown else "FAILED"
                op.error = error
                break

    # -- registry bridge --
    def _get_job(self, job_id: str) -> Optional[dict]:
        try:
            return self.registry.get(job_id)
        except Exception:
            return None

    def _job_transition_safe(self, plan: MigrationPlan, to_state: str, **kwargs):
        transition = getattr(self.registry, "transition", None)
        try:
            if callable(transition):
                return transition(plan.job_id, to_state, **kwargs)
            update = getattr(self.registry, "update", None)
            if callable(update):
                attrs = dict(kwargs)
                attrs.pop("expected_version", None)
                attrs.pop("expected_epoch", None)
                attrs.pop("ownership_change", None)
                attrs.pop("clear_active_migration", None)
                if to_state == "RUNNING" and kwargs.get("ownership_change"):
                    job = self._get_job(plan.job_id) or {}
                    attrs["execution_epoch"] = (job.get("execution_epoch", 0) or 0) + 1
                    attrs["active_migration_id"] = None
                elif "active_migration_id" in kwargs:
                    attrs["active_migration_id"] = kwargs["active_migration_id"]
                return update(plan.job_id, to_state, **attrs)
            logger.warning("Registry has no transition/update; skipping job %s", to_state)
        except Exception as exc:
            logger.error("Registry transition %s failed: %s", to_state, exc)
            raise MigrationFailed(f"Registry transition to {to_state} failed: {exc}")

    # -- terminal handlers --
    def _handle_abort(self, reason: str):
        state = self._execution_state
        if state.current_state in _POST_FENCE_STATES:
            # Abort is not rollback after fencing (Protocols #4 §6.3).
            self._handle_failure(f"abort requested post-fencing: {reason}")
            return
        state.abort_requested = True
        self._transition_to(MigrationState.ABORTING)

        try:
            cleanup = getattr(self.cleanup_executor, "cleanup_migration", None)
            if callable(cleanup):
                cleanup(state.plan, state.operations)
            self._transition_to(MigrationState.ABORTED)
        except Exception as e:
            logger.error(f"Abort cleanup failed: {e}")
            self._transition_to(MigrationState.FAILED)

        try:
            self._job_transition_safe(state.plan, "RUNNING")
        except MigrationFailed:
            logger.error("Abort job-state restore failed")
        finally:
            self._release_pool_slot()

    def _handle_superseded(self, reason: str):
        self._execution_state.superseded_by = reason
        self._transition_to(MigrationState.ABORTING)

        try:
            cleanup = getattr(self.cleanup_executor, "cleanup_migration", None)
            if callable(cleanup):
                try:
                    cleanup(self._execution_state.plan,
                            self._execution_state.operations,
                            safety_critical_only=True)
                except TypeError:
                    cleanup(self._execution_state.plan, self._execution_state.operations)
            self._transition_to(MigrationState.SUPERSEDED)
        except Exception as e:
            logger.error(f"Superseded cleanup failed: {e}")
            self._transition_to(MigrationState.FAILED)
        finally:
            self._release_pool_slot()

    def _handle_failure(self, reason: str):
        state = self._execution_state
        post_fencing = state.fencing_confirmed or state.current_state in _POST_FENCE_STATES
        fencing_phase = state.current_state == MigrationState.FENCING

        if fencing_phase:
            # FENCING_FAILED → reconciliation when the state exists.
            try:
                self._job_transition_safe(state.plan, "RECONCILIATION_REQUIRED")
            except MigrationFailed:
                try:
                    self._job_transition_safe(state.plan, "RECOVERY_REQUIRED")
                except MigrationFailed:
                    pass
        elif post_fencing:
            try:
                self._job_transition_safe(state.plan, "RECOVERY_REQUIRED")
            except MigrationFailed:
                pass
        else:
            # Pre-fence: FULL_ROLLBACK to RUNNING when no durable
            # checkpoint exists, else RECOVERY_REQUIRED.
            if self._has_durable_checkpoint(state):
                try:
                    self._job_transition_safe(state.plan, "RECOVERY_REQUIRED")
                except MigrationFailed:
                    pass
            else:
                try:
                    cleanup = getattr(self.cleanup_executor, "cleanup_migration", None)
                    if callable(cleanup):
                        cleanup(state.plan, state.operations)
                finally:
                    try:
                        self._job_transition_safe(state.plan, "RUNNING")
                    except MigrationFailed:
                        pass

        self._release_pool_slot()
        try:
            self._audit("MIGRATION_FAILED",
                        outcome="FAILED",
                        reason=reason,
                        fencing_confirmed=state.fencing_confirmed)
        except Exception as exc:
            logger.error("MIGRATION_FAILED audit write failed: %s", exc)
        self._transition_to(MigrationState.FAILED)

    def _has_durable_checkpoint(self, state: MigrationExecutionState) -> bool:
        if self.checkpoint_store is None:
            return False
        try:
            if state.checkpoint_id:
                doc = self.checkpoint_store.get(state.checkpoint_id)
                if doc.get("durability") in ("DURABLE", "VALIDATED"):
                    return True
        except Exception:
            pass
        try:
            any_durable = getattr(self.checkpoint_store, "any_durable", None)
            if callable(any_durable):
                return bool(any_durable(state.plan.job_id))
        except Exception:
            pass
        return False

    def _handle_deadline_exceeded(self, reason: str):
        state = self._execution_state
        if state.current_state in _POST_FENCE_STATES:
            logger.warning("Deadline exceeded in post-fencing phase; continuing forward")
        else:
            self._handle_failure(reason)


class MigrationAborted(Exception):
    pass


class MigrationSuperseded(Exception):
    pass


class MigrationFailed(Exception):
    pass


class OperationUnknownError(Exception):
    """An external operation's outcome cannot be determined (not a failure)."""


class DeadlineExceeded(Exception):
    pass
