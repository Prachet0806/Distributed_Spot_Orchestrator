from dataclasses import dataclass
from typing import Optional, Any
from orchestrator.protocol import Query, QueryType


@dataclass
class QueryResult:
    success: bool
    data: Any = None
    error: Optional[str] = None


class QueryHandler:
    def __init__(self):
        self.handlers = {}

    def register(self, query_type: QueryType, handler: callable):
        self.handlers[query_type] = handler

    def handle(self, query: Query) -> QueryResult:
        handler = self.handlers.get(query.query_type)
        if not handler:
            return QueryResult(success=False, error=f"No handler for query type: {query.query_type}")
        try:
            return QueryResult(success=True, data=handler(query))
        except Exception as e:
            return QueryResult(success=False, error=str(e))


@dataclass
class CandidateReadinessResult:
    pool_id: str
    readiness_status: str
    assessment_id: str
    assessed_at: str
    snapshot_version: str


@dataclass
class OperationStatusResult:
    operation_id: str
    state: str
    result: Any = None
    error: Optional[str] = None
    completed_at: Optional[str] = None


@dataclass
class CheckpointStatusResult:
    checkpoint_id: str
    status: str
    size_bytes: Optional[int] = None
    digest: Optional[str] = None


@dataclass
class ExecutionOwnershipResult:
    job_id: str
    execution_epoch: int
    authoritative_owner: str
    active_migration_id: Optional[str] = None


@dataclass
class InstanceStateResult:
    instance_id: str
    state: str
    public_ip: Optional[str] = None


# Query classes
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