from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable

from omniagent.models import TaskEvent
from omniagent.storage import SQLiteStore

EventHandler = Callable[[TaskEvent], None]


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, list[EventHandler]] = defaultdict(list)

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        self._subscribers[event_type].append(handler)

    def publish(self, event: TaskEvent) -> None:
        for handler in self._subscribers.get("*", []):
            handler(event)
        for handler in self._subscribers.get(event.event_type, []):
            handler(event)


class EventRecorder:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def __call__(self, event: TaskEvent) -> None:
        self.store.append_event(event)


class EventCollector:
    def __init__(self) -> None:
        self.events: list[TaskEvent] = []

    def __call__(self, event: TaskEvent) -> None:
        self.events.append(event)
