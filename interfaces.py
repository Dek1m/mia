"""Interfaces — Port/Adapter контракты для Mia.

Все компоненты системы реализуют эти интерфейсы.
Зависимости идут ТОЛЬКО от interfaces.py (Dependency Rule).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable


class ICache(ABC):
    """Кеш интерфейс."""

    @abstractmethod
    def get(self, key: str) -> Any | None: ...

    @abstractmethod
    def set(self, key: str, value: Any, ttl: int | None = None) -> None: ...

    @abstractmethod
    def delete(self, key: str) -> bool: ...

    @abstractmethod
    def exists(self, key: str) -> bool: ...

    @abstractmethod
    def clear(self) -> None: ...


class IThreadPool(ABC):
    """Пул потоков."""

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def submit(self, fn: Callable, *args: Any, **kwargs: Any) -> Any: ...

    @abstractmethod
    def shutdown(self, wait: bool = True) -> None: ...


class IProcessPool(ABC):
    """Пул процессов."""

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def submit(self, fn: Callable, *args: Any, timeout: float | None = None, **kwargs: Any) -> Any: ...

    @abstractmethod
    def shutdown(self, timeout: float = 5.0) -> None: ...


class IEventBus(ABC):
    """Шина событий."""

    @abstractmethod
    def subscribe(self, event: str, handler: Callable) -> None: ...

    @abstractmethod
    def unsubscribe(self, event: str, handler: Callable) -> None: ...

    @abstractmethod
    def publish(self, event: str, data: Any = None) -> None: ...

    @abstractmethod
    def clear(self) -> None: ...


class IHeartbeatMonitor(ABC):
    """Мониторинг heartbeat."""

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def register(self, pid: int) -> None: ...

    @abstractmethod
    def unregister(self, pid: int) -> None: ...

    @abstractmethod
    def update(self, pid: int) -> None: ...


class IModuleRegistry(ABC):
    """Реестр модулей."""

    @abstractmethod
    def discover(self) -> list[str]: ...

    @abstractmethod
    def load(self, name: str, state: Any = None) -> Any: ...

    @abstractmethod
    def unload(self, name: str) -> None: ...

    @abstractmethod
    def get(self, name: str) -> Any | None: ...

    @abstractmethod
    def list_all(self) -> list[str]: ...


class IServiceProvider(ABC):
    """Провайдер сервисов (DI)."""

    @abstractmethod
    def register(self, interface: type, implementation: Any) -> None: ...

    @abstractmethod
    def resolve(self, interface: type) -> Any: ...

    @abstractmethod
    def has(self, interface: type) -> bool: ...


class IShutdownManager(ABC):
    """Менеджер завершения."""

    @abstractmethod
    def register_hook(self, hook: Callable) -> None: ...

    @abstractmethod
    def shutdown(self, timeout: float = 30.0) -> None: ...
