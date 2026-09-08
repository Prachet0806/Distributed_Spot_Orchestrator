from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, List, Dict
import uuid
import logging

logger = logging.getLogger(__name__)


class ProcessingStatus(str, Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass
class EventLedgerEntry:
    event_id: str
    consumer_id: str
    status: ProcessingStatus = ProcessingStatus.PENDING
    received_at: datetime = field(default_factory=datetime.utcnow)
    processed_at: Optional[datetime] = None
    attempt_count: int = 0
    last_error: Optional[str] = None


class EventLedger:
    def __init__(self, table_name: str = None, dynamodb_resource: any = None):
        self.table_name = table_name or "spot_arbitrage_event_ledger"
        self.dynamodb = dynamodb_resource
        self._local_cache: Dict[str, Dict[str, EventLedgerEntry]] = {}

    def record(self, event_id: str, consumer_id: str) -> bool:
        key = (event_id, consumer_id)
        if self.dynamodb:
            table = self.dynamodb.Table(self.table_name)
            try:
                table.put_item(
                    Item={
                        "event_id": event_id,
                        "consumer_id": consumer_id,
                        "status": "PENDING",
                        "received_at": datetime.utcnow().isoformat(),
                        "attempt_count": 0,
                    },
                    ConditionExpression="attribute_not_exists(event_id) AND attribute_not_exists(consumer_id)",
                )
                return True
            except Exception as e:
                if "ConditionalCheckFailedException" in str(e):
                    return False
                raise
        else:
            if event_id not in self._local_cache:
                self._local_cache[event_id] = {}
            if consumer_id in self._local_cache[event_id]:
                return False
            self._local_cache[event_id][consumer_id] = EventLedgerEntry(
                event_id=event_id,
                consumer_id=consumer_id,
            )
            return True

    def mark_processing(self, event_id: str, consumer_id: str):
        if self.dynamodb:
            table = self.dynamodb.Table(self.table_name)
            table.update_item(
                Key={"event_id": event_id, "consumer_id": consumer_id},
                UpdateExpression="SET #s = :p, attempt_count = attempt_count + :inc",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":p": "PROCESSING", ":inc": 1},
            )
        else:
            entry = self._local_cache.get(event_id, {}).get(consumer_id)
            if entry:
                entry.status = ProcessingStatus.PROCESSING
                entry.attempt_count += 1

    def mark_completed(self, event_id: str, consumer_id: str):
        if self.dynamodb:
            table = self.dynamodb.Table(self.table_name)
            table.update_item(
                Key={"event_id": event_id, "consumer_id": consumer_id},
                UpdateExpression="SET #s = :c, processed_at = :t",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":c": "COMPLETED",
                    ":t": datetime.utcnow().isoformat(),
                },
            )
        else:
            entry = self._local_cache.get(event_id, {}).get(consumer_id)
            if entry:
                entry.status = ProcessingStatus.COMPLETED
                entry.processed_at = datetime.utcnow()

    def mark_failed(self, event_id: str, consumer_id: str, error: str):
        if self.dynamodb:
            table = self.dynamodb.Table(self.table_name)
            table.update_item(
                Key={"event_id": event_id, "consumer_id": consumer_id},
                UpdateExpression="SET #s = :f, last_error = :e",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":f": "FAILED", ":e": error},
            )
        else:
            entry = self._local_cache.get(event_id, {}).get(consumer_id)
            if entry:
                entry.status = ProcessingStatus.FAILED
                entry.last_error = error

    def has_processed(self, event_id: str, consumer_id: str) -> bool:
        if self.dynamodb:
            table = self.dynamodb.Table(self.table_name)
            resp = table.get_item(Key={"event_id": event_id, "consumer_id": consumer_id})
            item = resp.get("Item")
            if item:
                return item.get("status") == "COMPLETED"
            return False
        else:
            entry = self._local_cache.get(event_id, {}).get(consumer_id)
            return entry and entry.status == ProcessingStatus.COMPLETED

    def record_and_check(self, event_id: str, consumer_id: str) -> bool:
        recorded = self.record(event_id, consumer_id)
        return recorded