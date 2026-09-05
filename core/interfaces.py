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


class ISmartDispatcher(ABC):
    """Интерфейс SmartDispatcher — контракт для маршрутизатора задач."""

    @abstractmethod
    def dispatch(self, first: Any, *args: Any, **kwargs: Any) -> Any:
        """Маршрутизировать и выполнить задачу (blocking)."""
        ...

    @abstractmethod
    def dispatch_async(self, first: Any, *args: Any, **kwargs: Any) -> Any:
        """Маршрутизировать задачу (non-blocking, возвращает Future).

        Args:
            first: Task-объект или функция для выполнения.
            *args: Аргументы функции.
            **kwargs: Именованные аргументы функции.

        Returns:
            Future с результатом выполнения.
        """
        ...

    @abstractmethod
    def acquire_lock(self) -> None:
        """Захватить блокировку записей."""
        ...

    @abstractmethod
    def release_lock(self) -> None:
        """Освободить блокировку записей."""
        ...


class ILogger(ABC):
    """Интерфейс логгера — контракт для всех логгеров."""

    @abstractmethod
    def info(self, message: str, **kwargs: Any) -> None: ...

    @abstractmethod
    def warning(self, message: str, **kwargs: Any) -> None: ...

    @abstractmethod
    def error(self, message: str, **kwargs: Any) -> None: ...

    @abstractmethod
    def exception(self, message: str, **kwargs: Any) -> None:
        """ERROR с traceback текущего исключения (вызывать внутри except)."""

    @abstractmethod
    def debug(self, message: str, **kwargs: Any) -> None: ...

    @abstractmethod
    def critical(self, message: str, **kwargs: Any) -> None: ...

    @abstractmethod
    def child(self, name: str) -> "ILogger": ...


class IDatabase(ABC):
    """Интерфейс Database — контракт для провайдеров."""

    @abstractmethod
    def register_provider(self, name: str, provider: Any, is_default: bool = False) -> None: ...

    @abstractmethod
    def get_provider(self, name: str | None = None) -> Any: ...

    @abstractmethod
    def get(self, table: str, id: str) -> dict | None: ...

    @abstractmethod
    def get_by_field(self, table: str, field: str, value: Any) -> dict | None: ...

    @abstractmethod
    def insert(self, table: str, data: dict) -> str: ...

    @abstractmethod
    def update(self, table: str, id: str, data: dict) -> dict | None: ...

    @abstractmethod
    def delete(self, table: str, id: str) -> bool: ...

    @abstractmethod
    def exists(self, table: str, id: str) -> bool: ...

    @abstractmethod
    def count(self, table: str, filters: dict | None = None) -> int: ...

    @abstractmethod
    def list(self, table: str, filters: dict | None = None, limit: int = 100, offset: int = 0) -> list[dict]: ...

    @abstractmethod
    def fetch(self, query: str, *params: Any) -> list[dict]: ...

    @abstractmethod
    def execute(self, query: str, *params: Any) -> str: ...
