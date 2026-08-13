"""ServiceRegistry — простое хранилище сервисов для DI."""
from __future__ import annotations

import threading
from typing import Any

from argenta_logging import get_logger
from core.interfaces import IServiceProvider

log = get_logger(__name__)


class ServiceRegistry(IServiceProvider):
    """Реализация IServiceProvider на основе dict."""

    def __init__(self) -> None:
        self._services: dict[type, Any] = {}
        self._lock = threading.RLock()

    def register(self, interface: type, implementation: Any) -> None:
        with self._lock:
            self._services[interface] = implementation
            log.debug("Service registered", extra={"interface": interface.__name__})

    def resolve(self, interface: type) -> Any:
        with self._lock:
            if interface not in self._services:
                raise KeyError(f"Service not registered: {interface.__name__}")
            return self._services[interface]

    def has(self, interface: type) -> bool:
        with self._lock:
            return interface in self._services
