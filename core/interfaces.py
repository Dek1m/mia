"""Interfaces — Port/Adapter контракты для Mia.

Все компоненты системы реализуют эти интерфейсы.
Зависимости идут ТОЛЬКО от interfaces.py (Dependency Rule).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable
from uuid import UUID


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


class ICpuMetricsCollector(ABC):
    """Сбор метрик CPU."""

    @abstractmethod
    def get_cpu_load(self) -> float: ...

    @abstractmethod
    def get_per_core_load(self) -> list[float]: ...

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...


class ILoadBalancer(ABC):
    """Балансировщик нагрузки."""

    @abstractmethod
    def select_worker(self, workers: dict[int, Any] | None = None) -> int | None: ...

    @abstractmethod
    def update_worker_state(self, worker_id: int, state: Any) -> None: ...

    @abstractmethod
    def increment_active(self, worker_id: int) -> None: ...

    @abstractmethod
    def decrement_active(self, worker_id: int) -> None: ...


class IWorkerManager(ABC):
    """Управление lifecycle воркеров."""

    @abstractmethod
    def start(self, num_workers: int | None = None) -> None: ...

    @abstractmethod
    def stop(self, timeout: float = 5.0) -> None: ...

    @abstractmethod
    def restart_worker(self, worker_id: int) -> None: ...

    @abstractmethod
    def get_worker_ids(self) -> list[int]: ...

    @abstractmethod
    def submit(self, fn: Callable, *args: Any, timeout: float | None = None, **kwargs: Any) -> Any: ...


class ISmartDispatcher(ABC):
    """Интерфейс SmartDispatcher — контракт для маршрутизатора задач."""

    @abstractmethod
    def dispatch(self, first: Any, *args: Any, **kwargs: Any) -> Any:
        """Маршрутизировать и выполнить задачу (sync)."""
        ...

    @abstractmethod
    def dispatch_async(self, first: Any, *args: Any, **kwargs: Any) -> Any:
        """Маршрутизировать задачу асинхронно.

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

    @property
    @abstractmethod
    def metrics(self) -> dict[str, int]:
        """Количество выполненных задач по типам."""
        ...


class ISharedMemory(ABC):
    """Хранилище результатов задач по UUID."""

    @abstractmethod
    def set(self, task_id: UUID, result: Any) -> None:
        """Сохранить результат задачи."""
        ...

    @abstractmethod
    def get(self, task_id: UUID) -> Any | None:
        """Получить результат задачи."""
        ...

    @abstractmethod
    def delete(self, task_id: UUID) -> bool:
        """Удалить результат задачи."""
        ...

    @abstractmethod
    def exists(self, task_id: UUID) -> bool:
        """Проверить наличие результата."""
        ...

    @abstractmethod
    def clear(self) -> None:
        """Очистить все результаты."""
        ...

    @abstractmethod
    def shutdown(self) -> None:
        """Остановить хранилище."""
        ...


class IWorkerThreadPool(ABC):
    """Пул потоков внутри воркера."""

    @abstractmethod
    def start(self) -> None:
        """Запустить пул потоков."""
        ...

    @abstractmethod
    def submit(self, fn: Callable, *args: Any, **kwargs: Any) -> Any:
        """Отправить задачу на выполнение."""
        ...

    @abstractmethod
    def shutdown(self, wait: bool = True) -> None:
        """Остановить пул потоков."""
        ...

    @property
    @abstractmethod
    def active_count(self) -> int:
        """Количество активных задач."""
        ...


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
