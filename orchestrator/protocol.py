from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional, Dict
import uuid


class OperationState(str, Enum):
    ISSUED = "ISSUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class CommandType(str, Enum):
    PROVISION = "PROVISION"
    CHECKPOINT = "CHECKPOINT"
    TRANSFER = "TRANSFER"
    RESTORE = "RESTORE"
    FENCE = "FENCE"
    CLEANUP = "CLEANUP"
    MIGRATE = "MIGRATE"


class EventType(str, Enum):
    SPOT_INTERRUPTION = "SPOT_INTERRUPTION"
    PROVISIONING_COMPLETED = "PROVISIONING_COMPLETED"
    CHECKPOINT_PERSISTED = "CHECKPOINT_PERSISTED"
    SOURCE_TERMINATED = "SOURCE_TERMINATED"
    VALIDATION_COMPLETED = "VALIDATION_COMPLETED"
    INFRASTRUCTURE_MISMATCH = "INFRASTRUCTURE_MISMATCH"
    MIGRATION_COMPLETED = "MIGRATION_COMPLETED"
    RECONCILIATION_FINDING = "RECONCILIATION_FINDING"
    SPOT_PRICE_OBSERVATION = "SPOT_PRICE_OBSERVATION"


class QueryType(str, Enum):
    GET_CANDIDATE_READINESS = "GET_CANDIDATE_READINESS"
    GET_OPERATION_STATUS = "GET_OPERATION_STATUS"
    GET_CHECKPOINT_STATUS = "GET_CHECKPOINT_STATUS"
    GET_EXECUTION_OWNERSHIP = "GET_EXECUTION_OWNERSHIP"
    GET_INSTANCE_STATE = "GET_INSTANCE_STATE"
    GET_MIGRATION_PLAN = "GET_MIGRATION_PLAN"


@dataclass
class Command:
    command_id: str
    operation_id: str
    job_id: str
    execution_epoch: int
    migration_id: str
    correlation_id: str
    issued_at: datetime
    deadline: Optional[datetime] = None
    payload: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(cls, command_type: CommandType, **kwargs) -> "Command":
        cmd = cls(
            command_id=str(uuid.uuid4()),
            operation_id=kwargs.get("operation_id", str(uuid.uuid4())),
            job_id=kwargs["job_id"],
            execution_epoch=kwargs["execution_epoch"],
            migration_id=kwargs["migration_id"],
            correlation_id=kwargs["correlation_id"],
            issued_at=datetime.utcnow(),
            deadline=kwargs.get("deadline"),
            payload=kwargs.get("payload", {}),
        )
        cmd.payload["command_type"] = command_type.value
        return cmd


@dataclass
class ProvisionCommand(Command):
    pass


@dataclass
class CheckpointCommand(Command):
    pass


@dataclass
class TransferCommand(Command):
    pass


@dataclass
class RestoreCommand(Command):
    pass


@dataclass
class FenceCommand(Command):
    pass


@dataclass
class CleanupCommand(Command):
    pass


@dataclass
class Operation:
    operation_id: str
    command_id: str
    state: OperationState = OperationState.ISSUED
    started_at: datetime = field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None
    result: Any = None
    error: Optional[str] = None
    retries: int = 0

    def mark_running(self):
        self.state = OperationState.RUNNING

    def mark_succeeded(self, result: Any = None):
        self.state = OperationState.SUCCEEDED
        self.completed_at = datetime.utcnow()
        self.result = result

    def mark_failed(self, error: str):
        self.state = OperationState.FAILED
        self.completed_at = datetime.utcnow()
        self.error = error

    def mark_unknown(self, error: str = None):
        self.state = OperationState.UNKNOWN
        self.completed_at = datetime.utcnow()
        self.error = error


@dataclass
class Event:
    event_id: str
    event_type: EventType
    job_id: str
    execution_epoch: int
    migration_id: Optional[str]
    correlation_id: str
    occurred_at: datetime
    effective_at: datetime
    producer: str
    payload: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(cls, event_type: EventType, **kwargs) -> "Event":
        now = datetime.utcnow()
        return cls(
            event_id=str(uuid.uuid4()),
            event_type=event_type,
            job_id=kwargs["job_id"],
            execution_epoch=kwargs["execution_epoch"],
            migration_id=kwargs.get("migration_id"),
            correlation_id=kwargs["correlation_id"],
            occurred_at=now,
            effective_at=kwargs.get("effective_at", now),
            producer=kwargs["producer"],
            payload=kwargs.get("payload", {}),
        )


@dataclass
class ReconciliationEvent(Event):
    pass


@dataclass
class SpotInterruptionEvent(Event):
    pass


@dataclass
class ProvisioningCompletedEvent(Event):
    pass


@dataclass
class CheckpointPersistedEvent(Event):
    pass


@dataclass
class SourceTerminatedEvent(Event):
    pass


@dataclass
class ValidationCompletedEvent(Event):
    pass


@dataclass
class InfrastructureMismatchEvent(Event):
    pass


@dataclass
class MigrationCompletedEvent(Event):
    pass


@dataclass
class ReconciliationFindingEvent(Event):
    pass


@dataclass
class SpotPriceObservationEvent(Event):
    pass


class QueryType(str, Enum):
    GET_CANDIDATE_READINESS = "GET_CANDIDATE_READINESS"
    GET_OPERATION_STATUS = "GET_OPERATION_STATUS"
    GET_CHECKPOINT_STATUS = "GET_CHECKPOINT_STATUS"
    GET_EXECUTION_OWNERSHIP = "GET_EXECUTION_OWNERSHIP"
    GET_INSTANCE_STATE = "GET_INSTANCE_STATE"
    GET_MIGRATION_PLAN = "GET_MIGRATION_PLAN"


@dataclass
class Query:
    query_id: str
    query_type: QueryType
    correlation_id: str
    job_id: Optional[str] = None
    migration_id: Optional[str] = None
    issued_at: datetime = field(default_factory=datetime.utcnow)
    deadline: Optional[datetime] = None
    parameters: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(cls, query_type: QueryType, **kwargs) -> "Query":
        return cls(
            query_id=str(uuid.uuid4()),
            query_type=query_type,
            correlation_id=kwargs["correlation_id"],
            job_id=kwargs.get("job_id"),
            migration_id=kwargs.get("migration_id"),
            issued_at=datetime.utcnow(),
            deadline=kwargs.get("deadline"),
            parameters=kwargs.get("parameters", {}),
        )


@dataclass
class GetCandidateReadinessQuery(Query):
    pass


@dataclass
class GetOperationStatusQuery(Query):
    pass


@dataclass
class GetCheckpointStatusQuery(Query):
    pass


@dataclass
class GetExecutionOwnershipQuery(Query):
    pass


@dataclass
class GetInstanceStateQuery(Query):
    pass


@dataclass
class GetMigrationPlanQuery(Query):
    pass


@dataclass
class StateTransition:
    entity_id: str
    expected_version: int
    transition: Dict[str, Any]
    actor: str
    timestamp: datetime = field(default_factory=datetime.utcnow)
    execution_epoch: Optional[int] = None


class StateMutationAuthority(str, Enum):
    JOB_REGISTRY = "JOB_REGISTRY"
    MIGRATION_COORDINATOR = "MIGRATION_COORDINATOR"
    RECONCILIATION_MANAGER = "RECONCILIATION_MANAGER"
    CLEANUP_EXECUTOR = "CLEANUP_EXECUTOR"