from typing import Callable, Dict, List, Optional
from orchestrator.protocol import Event
from orchestrator.event_ledger import EventLedger
import logging

logger = logging.getLogger(__name__)


class EventConsumer:
    def __init__(self, consumer_id: str, handler: Callable):
        self.consumer_id = consumer_id
        self.handler = handler

    def can_handle(self, event_type: str) -> bool:
        return True

    def handle(self, event):
        return self.handler(event)


class EventDispatcher:
    def __init__(self, event_ledger: EventLedger = None):
        self.event_ledger = event_ledger or EventLedger()
        self.consumers: Dict[str, List[EventConsumer]] = {}
        self._default_consumers: List[EventConsumer] = []

    def register_consumer(self, event_type: str, consumer: EventConsumer):
        if event_type not in self.consumers:
            self.consumers[event_type] = []
        self.consumers[event_type].append(consumer)

    def register_default_consumer(self, consumer: EventConsumer):
        self._default_consumers.append(consumer)

    def dispatch(self, event):
        event_type = event.event_type.value if hasattr(event.event_type, 'value') else str(event.event_type)
        consumers = self.consumers.get(event_type, [])
        all_consumers = consumers + self._default_consumers

        for consumer in all_consumers:
            if not consumer.can_handle(event_type):
                continue

            recorded = self.event_ledger.record_and_check(event.event_id, consumer.consumer_id)
            if not recorded:
                logger.debug(f"Event {event.event_id} already processed by {consumer.consumer_id}, skipping")
                continue

            try:
                self.event_ledger.mark_processing(event.event_id, consumer.consumer_id)
                consumer.handle(event)
                self.event_ledger.mark_completed(event.event_id, consumer.consumer_id)
            except Exception as e:
                logger.error(f"Error processing event {event.event_id} by {consumer.consumer_id}: {e}")
                self.event_ledger.mark_failed(event.event_id, consumer.consumer_id, str(e))
                raise