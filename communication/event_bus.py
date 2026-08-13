"""Event Bus — события между модулями."""
import threading
from typing import Any, Callable
from collections import defaultdict

from argenta_logging import get_logger

log = get_logger(__name__)

# Константы событий
EVENT_PROCESS_DIED = "process.died"
EVENT_PROCESS_RESTARTED = "process.restarted"


class EventBus:
    """Шина событий для коммуникации между модулями."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Callable]] = defaultdict(list)
        self._lock = threading.RLock()

    def subscribe(self, event: str, handler: Callable) -> None:
        """Подписаться на событие."""
        with self._lock:
            self._subscribers[event].append(handler)
        log.debug("Subscribed to event", extra={"event": event, "handler": handler.__name__})

    def unsubscribe(self, event: str, handler: Callable) -> None:
        """Отписаться от события."""
        with self._lock:
            if event in self._subscribers:
                self._subscribers[event] = [
                    h for h in self._subscribers[event] if h != handler
                ]

    def publish(self, event: str, data: Any = None) -> None:
        """Опубликовать событие."""
        with self._lock:
            handlers = list(self._subscribers.get(event, []))
        log.info("Event published", extra={"event": event, "handlers_count": len(handlers)})
        for handler in handlers:
            try:
                handler(data)
            except Exception as e:
                log.error("Event handler error", extra={
                    "event": event,
                    "handler": handler.__name__,
                    "error": str(e),
                })

    def clear(self) -> None:
        """Очистить все подписки."""
        with self._lock:
            self._subscribers.clear()
