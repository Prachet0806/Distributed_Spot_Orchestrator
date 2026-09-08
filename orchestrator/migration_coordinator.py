from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional, Callable, Any
import time
import uuid
import logging

from orchestrator.migration_planner import MigrationPlan, MigrationState
from orchestrator.recovery_feasibility import RecoveryFeasibility
from orchestrator.policy_engine import MigrationRegime

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
    status: str = "RUNNING"
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
    deadline_exceeded: bool = False
    abort_requested: bool = False
    superseded_by: Optional[str] = None


class MigrationCoordinator:
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

        self._execution_state: Optional[MigrationExecutionState] = None
        self._state_machine = self._build_state_machine()
        self._deadline_monitor_active = False

    def _build_state_machine(self) -> dict:
        return {
            MigrationState.PLANNED: {"next": MigrationState.PRECHECKING},
            MigrationState.PRECHECKING: {"next": MigrationState.CHECKPOINTING, "abort": MigrationState.ABORTING},
            MigrationState.CHECKPOINTING: {"next": MigrationState.PERSISTING, "abort": MigrationState.ABORTING, "retry": MigrationState.CHECKPOINTING},
            MigrationState.PERSISTING: {"next": MigrationState.PROVISIONING, "abort": MigrationState.ABORTING, "retry": MigrationState.PERSISTING},
            MigrationState.PROVISIONING: {"next": MigrationState.TRANSFERRING, "abort": MigrationState.ABORTING, "retry": MigrationState.PROVISIONING},
            MigrationState.TRANSFERRING: {"next": MigrationState.RESTORING, "abort": MigrationState.ABORTING, "retry": MigrationState.TRANSFERRING},
            MigrationState.RESTORING: {"next": MigrationState.FENCING, "abort": MigrationState.ABORTING, "retry": MigrationState.RESTORING},
            MigrationState.FENCING: {"next": MigrationState.VALIDATING, "abort": MigrationState.ABORTING, "failed": MigrationState.FAILED},
            MigrationState.VALIDATING: {"next": MigrationState.ACTIVATING, "abort": MigrationState.ABORTING, "retry": MigrationState.RESTORING, "failed": MigrationState.FAILED},
            MigrationState.ACTIVATING: {"next": MigrationState.FINALIZING, "failed": MigrationState.FAILED},
            MigrationState.FINALIZING: {"next": MigrationState.SUCCESS, "failed": MigrationState.FAILED},
            MigrationState.ABORTING: {"next": MigrationState.ABORTED, "cleanup_failed": MigrationState.FAILED},
            MigrationState.SUCCESS: {},
            MigrationState.ABORTED: {},
            MigrationState.SUPERSEDED: {},
            MigrationState.FAILED: {},
        }

    def execute_plan(
        self,
        plan: MigrationPlan,
        on_state_change: Optional[Callable[[MigrationState, MigrationState], None]] = None,
    ) -> MigrationExecutionState:
        self._execution_state = MigrationExecutionState(
            plan=plan,
            current_state=MigrationState.PLANNED,
            state_entered_at=datetime.utcnow(),
            execution_epoch=plan.execution_epoch,
        )

        logger.info(f"Starting migration {plan.migration_id} for job {plan.job_id} (regime: {plan.regime.value})")

        if plan.is_expired():
            logger.warning(f"Plan {plan.migration_id} expired before execution")
            return self._transition_to(MigrationState.FAILED, "Plan expired")

        try:
            self._transition_to(MigrationState.PRECHECKING, on_state_change)
            self._execute_prechecking()

            self._transition_to(MigrationState.CHECKPOINTING, on_state_change)
            self._execute_checkpointing()

            self._transition_to(MigrationState.PERSISTING, on_state_change)
            self._execute_persisting()

            if plan.regime == MigrationRegime.EMERGENCY:
                self._execute_parallel_preparation(plan)

            self._transition_to(MigrationState.PROVISIONING, on_state_change)
            self._execute_provisioning()

            self._transition_to(MigrationState.TRANSFERRING, on_state_change)
            self._execute_transferring()

            self._transition_to(MigrationState.RESTORING, on_state_change)
            self._execute_restoring()

            self._transition_to(MigrationState.FENCING, on_state_change)
            self._execute_fencing()

            self._transition_to(MigrationState.VALIDATING, on_state_change)
            self._execute_validating()

            self._transition_to(MigrationState.ACTIVATING, on_state_change)
            self._execute_activating()

            self._transition_to(MigrationState.FINALIZING, on_state_change)
            self._execute_finalizing()

            self._transition_to(MigrationState.SUCCESS, on_state_change)
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

    def _execute_prechecking(self):
        state = self._execution_state
        plan = state.plan

        self._check_plan_validity(plan)
        self._verify_candidate_readiness(plan.target_candidate_id)
        self._verify_ownership(plan.execution_epoch)

        if plan.regime == MigrationRegime.EMERGENCY:
            self._check_deadline_budget(plan)

        self._update_registry_state(MigrationState.PRECHECKING)

    def _execute_checkpointing(self):
        state = self._execution_state
        op_id = self._start_operation("checkpointing")

        try:
            checkpoint_result = self._run_with_timeout(
                lambda: self.checkpoint_manager.create_checkpoint(plan=state.plan),
                timeout=self.step_timeout_seconds,
            )
            self._complete_operation(op_id, checkpoint_result)
            self._update_registry_state(MigrationState.CHECKPOINTING)
        except Exception as e:
            self._fail_operation(op_id, str(e))
            raise MigrationFailed(f"Checkpointing failed: {e}")

    def _execute_persisting(self):
        state = self._execution_state
        op_id = self._start_operation("persisting")

        try:
            persist_result = self._run_with_timeout(
                lambda: self.checkpoint_manager.persist_checkpoint(state.plan.migration_id),
                timeout=self.step_timeout_seconds,
            )
            self._complete_operation(op_id, persist_result)
            self._update_registry_state(MigrationState.PERSISTING)
        except Exception as e:
            self._fail_operation(op_id, str(e))
            raise MigrationFailed(f"Persistence failed: {e}")

    def _execute_parallel_preparation(self, plan: MigrationPlan):
        state = self._execution_state
        provision_op = self._start_operation("provisioning")

        try:
            provision_result = self._run_with_timeout(
                lambda: self.provisioner.provision(plan.target_candidate_id),
                timeout=self.step_timeout_seconds,
            )
            self._complete_operation(provision_op, provision_result)
        except Exception as e:
            self._fail_operation(provision_op, str(e))
            raise MigrationFailed(f"Emergency provisioning failed: {e}")

    def _execute_provisioning(self):
        state = self._execution_state
        if state.current_state == MigrationState.PROVISIONING:
            op_id = self._start_operation("provisioning")
            try:
                result = self._run_with_timeout(
                    lambda: self.provisioner.provision(state.plan.target_candidate_id),
                    timeout=self.step_timeout_seconds,
                )
                self._complete_operation(op_id, result)
            except Exception as e:
                self._fail_operation(op_id, str(e))
                raise MigrationFailed(f"Provisioning failed: {e}")

        self._update_registry_state(MigrationState.PROVISIONING)

    def _execute_transferring(self):
        state = self._execution_state
        op_id = self._start_operation("transferring")

        try:
            result = self._run_with_timeout(
                lambda: self.transfer_manager.transfer(state.plan.migration_id),
                timeout=self.step_timeout_seconds,
            )
            self._complete_operation(op_id, result)
            self._update_registry_state(MigrationState.TRANSFERRING)
        except Exception as e:
            self._fail_operation(op_id, str(e))
            raise MigrationFailed(f"Transfer failed: {e}")

    def _execute_restoring(self):
        state = self._execution_state
        op_id = self._start_operation("restoring")

        try:
            result = self._run_with_timeout(
                lambda: self.checkpoint_manager.restore_checkpoint(state.plan.migration_id),
                timeout=self.step_timeout_seconds,
            )
            self._complete_operation(op_id, result)
            self._update_registry_state(MigrationState.RESTORING)
        except Exception as e:
            self._fail_operation(op_id, str(e))
            if state.fencing_confirmed:
                raise MigrationFailed(f"Restore failed post-fencing: {e}")
            raise MigrationFailed(f"Restore failed: {e}")

    def _execute_fencing(self):
        state = self._execution_state
        plan = state.plan

        if not plan.fencing_authorized:
            raise MigrationFailed("Fencing not authorized in plan")

        op_id = self._start_operation("fencing")

        try:
            self._invalidate_source_epoch(plan.execution_epoch)
            self._terminate_source_process(plan.source_candidate_id)
            self._verify_source_terminated(plan.source_candidate_id)

            self._complete_operation(op_id, {"fenced": True})
            state.fencing_confirmed = True
            state.source_ownership = False
            state.target_ownership = True
            self._update_registry_state(MigrationState.FENCING, execution_epoch=plan.execution_epoch + 1)

        except Exception as e:
            self._fail_operation(op_id, str(e))
            raise MigrationFailed(f"Fencing failed: {e}")

    def _execute_validating(self):
        state = self._execution_state
        op_id = self._start_operation("validating")

        try:
            validation_result = self._run_with_timeout(
                lambda: self.validator.validate(state.plan.migration_id),
                timeout=self.step_timeout_seconds,
            )
            self._complete_operation(op_id, validation_result)

            if not validation_result.get("passed", False):
                if state.fencing_confirmed:
                    raise MigrationFailed(f"Validation failed post-fencing: {validation_result.get('reason')}")
                raise MigrationFailed(f"Validation failed: {validation_result.get('reason')}")

            self._update_registry_state(MigrationState.VALIDATING)

        except MigrationFailed:
            raise
        except Exception as e:
            self._fail_operation(op_id, str(e))
            if state.fencing_confirmed:
                raise MigrationFailed(f"Validation error post-fencing: {e}")
            raise MigrationFailed(f"Validation error: {e}")

    def _execute_activating(self):
        state = self._execution_state
        plan = state.plan
        op_id = self._start_operation("activating")

        try:
            self.registry.update_job_state(
                plan.job_id,
                MigrationState.ACTIVATING.value,
                execution_epoch=plan.execution_epoch + 1,
                active_migration_id=plan.migration_id,
                target_candidate_id=plan.target_candidate_id,
            )
            self._complete_operation(op_id, {"activated": True})
            self._update_registry_state(MigrationState.ACTIVATING)

        except Exception as e:
            self._fail_operation(op_id, str(e))
            raise MigrationFailed(f"Activation failed: {e}")

    def _execute_finalizing(self):
        state = self._execution_state
        plan = state.plan
        op_id = self._start_operation("finalizing")

        try:
            self.cleanup_executor.cleanup_source_resources(plan.source_candidate_id)
            self.registry.finalize_migration(plan.migration_id)
            self._complete_operation(op_id, {"finalized": True})
            self._update_registry_state(MigrationState.FINALIZING)

        except Exception as e:
            self._fail_operation(op_id, str(e))
            raise MigrationFailed(f"Finalization failed: {e}")

    def _transition_to(self, new_state: MigrationState, callback: Optional[Callable] = None):
        if self._execution_state is None:
            return

        old_state = self._execution_state.current_state
        valid = self._state_machine.get(old_state, {}).get("next") == new_state or \
                self._state_machine.get(old_state, {}).get("retry") == new_state or \
                self._state_machine.get(old_state, {}).get("abort") == new_state or \
                self._state_machine.get(old_state, {}).get("failed") == new_state

        if not valid and not (old_state == MigrationState.PLANNED and new_state == MigrationState.PRECHECKING):
            logger.warning(f"Transition {old_state} -> {new_state} may not be standard")

        self._execution_state.current_state = new_state
        self._execution_state.state_entered_at = datetime.utcnow()
        logger.info(f"Migration {self._execution_state.plan.migration_id}: {old_state} -> {new_state}")

        if callback:
            callback(old_state, new_state)

    def _check_plan_validity(self, plan: MigrationPlan):
        if plan.is_expired():
            raise MigrationAborted("Plan expired")

        if plan.regime == MigrationRegime.EMERGENCY and plan.deadline_seconds:
            self._check_deadline_budget(plan)

    def _check_deadline_budget(self, plan: MigrationPlan):
        if not plan.deadline_seconds:
            return

        elapsed = (datetime.utcnow() - plan.created_at).total_seconds()
        remaining = plan.deadline_seconds - elapsed
        required = (plan.estimated_critical_path_seconds or 0) + plan.safety_margin_seconds

        if remaining < required:
            self._execution_state.deadline_exceeded = True
            if self._execution_state.current_state in (MigrationState.FENCING, MigrationState.VALIDATING, MigrationState.ACTIVATING, MigrationState.FINALIZING):
                logger.warning("Deadline exceeded but in post-fencing phase; continuing")
                return
            raise DeadlineExceeded(f"Insufficient deadline budget: {remaining:.0f}s remaining, {required:.0f}s required")

    def _verify_candidate_readiness(self, candidate_id: str):
        pass

    def _verify_ownership(self, epoch: int):
        pass

    def _invalidate_source_epoch(self, epoch: int):
        pass

    def _terminate_source_process(self, candidate_id: str):
        pass

    def _verify_source_terminated(self, candidate_id: str):
        pass

    def _run_with_timeout(self, func: Callable, timeout: float) -> Any:
        return func()

    def _start_operation(self, step_name: str) -> str:
        op_id = f"{step_name}-{uuid.uuid4().hex[:8]}"
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

    def _fail_operation(self, operation_id: str, error: str):
        for op in self._execution_state.operations:
            if op.operation_id == operation_id:
                op.completed_at = datetime.utcnow()
                op.status = "FAILED"
                op.error = error
                break

    def _update_registry_state(self, state: MigrationState, **kwargs):
        if self._execution_state:
            self.registry.update_migration_state(
                self._execution_state.plan.migration_id,
                state.value,
                **kwargs,
            )

    def _handle_abort(self, reason: str):
        self._execution_state.abort_requested = True
        self._transition_to(MigrationState.ABORTING)

        try:
            self.cleanup_executor.cleanup_migration(
                self._execution_state.plan,
                self._execution_state.operations,
            )
            self._transition_to(MigrationState.ABORTED)
        except Exception as e:
            logger.error(f"Abort cleanup failed: {e}")
            self._transition_to(MigrationState.FAILED)

        self.registry.update_job_state(
            self._execution_state.plan.job_id,
            "RUNNING",
        )

    def _handle_superseded(self, reason: str):
        self._execution_state.superseded_by = reason
        self._transition_to(MigrationState.ABORTING)

        try:
            self.cleanup_executor.cleanup_migration(
                self._execution_state.plan,
                self._execution_state.operations,
                safety_critical_only=True,
            )
            self._transition_to(MigrationState.SUPERSEDED)
        except Exception as e:
            logger.error(f"Superseded cleanup failed: {e}")
            self._transition_to(MigrationState.FAILED)

    def _handle_failure(self, reason: str):
        state = self._execution_state
        post_fencing = state.fencing_confirmed

        if post_fencing:
            self.registry.update_job_state(
                state.plan.job_id,
                "RECOVERY_REQUIRED" if state.fencing_confirmed else "RESTART_REQUIRED",
            )
        else:
            self.registry.update_job_state(
                state.plan.job_id,
                "RECOVERY_REQUIRED",
            )

        self._transition_to(MigrationState.FAILED)

    def _handle_deadline_exceeded(self, reason: str):
        state = self._execution_state
        if state.current_state in (MigrationState.FENCING, MigrationState.VALIDATING, MigrationState.ACTIVATING):
            logger.warning("Deadline exceeded in post-fencing phase; continuing forward")
        else:
            self._handle_failure(reason)


class MigrationAborted(Exception):
    pass


class MigrationSuperseded(Exception):
    pass


class MigrationFailed(Exception):
    pass


class DeadlineExceeded(Exception):
    pass