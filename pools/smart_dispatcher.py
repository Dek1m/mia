"""SmartDispatcher — маршрутизатор задач по типам БД."""
from __future__ import annotations

import threading
from typing import Any, Callable

from argenta_logging import get_logger
from monitoring.metrics import (
    threadpool_tasks_submitted_total,
    worker_manager_tasks_submitted_total,
)

log = get_logger(__name__)

# Типы задач и стратегии маршрутизации
_TYPE_THREAD = frozenset({"read", "write", "transaction"})
_TYPE_AGGREGATE = "aggregate"


class SmartDispatcher:
    """Маршрутизатор задач по типам БД.

    Читает ``fn._db_type`` и направляет задачу в нужный пул:
      - read / write / transaction → ThreadPool
      - aggregate → WorkerManager

    Для write-задач с ``fn._db_lock = True`` используется
    общая блокировка, гарантирующая последовательность записей.
    """

    def __init__(self, thread_pool: Any, worker_manager: Any) -> None:
        self._thread_pool = thread_pool
        self._worker_manager = worker_manager
        self._write_lock = threading.Lock()
        self._metrics: dict[str, int] = {
            "read": 0,
            "write": 0,
            "aggregate": 0,
            "transaction": 0,
        }

    # === Публичный API ===

    def dispatch(self, fn: Callable, *args: Any, **kwargs: Any) -> Any:
        """Маршрутизировать и выполнить задачу.

        Определяет тип по ``fn._db_type`` (по умолчанию 'read').
        """
        db_type = getattr(fn, "_db_type", "read")

        if db_type == _TYPE_AGGREGATE:
            return self._dispatch_aggregate(fn, *args, **kwargs)

        if db_type in _TYPE_THREAD:
            return self._dispatch_thread(db_type, fn, *args, **kwargs)

        log.warning("Unknown db_type, fallback to read", extra={"db_type": db_type})
        return self._dispatch_thread("read", fn, *args, **kwargs)

    def acquire_lock(self) -> None:
        """Захватить блокировку записей (для ручного управления)."""
        self._write_lock.acquire()

    def release_lock(self) -> None:
        """Освободить блокировку записей."""
        self._write_lock.release()

    @property
    def metrics(self) -> dict[str, int]:
        """Количество выполненных задач по типам."""
        return dict(self._metrics)

    # === Внутренняя логика ===

    def _dispatch_thread(
        self, db_type: str, fn: Callable, *args: Any, **kwargs: Any,
    ) -> Any:
        """Отправить задачу в ThreadPool."""
        if db_type == "write" and getattr(fn, "_db_lock", False):
            with self._write_lock:
                self._metrics["write"] += 1
                threadpool_tasks_submitted_total.labels(status="ok").inc()
                log.debug(
                    "Dispatched write (locked)",
                    extra={"fn": fn.__name__},
                )
                return self._thread_pool.submit(fn, *args, **kwargs)

        self._metrics[db_type] += 1
        threadpool_tasks_submitted_total.labels(status="ok").inc()
        log.debug(
            "Dispatched to thread pool",
            extra={"fn": fn.__name__, "db_type": db_type},
        )
        return self._thread_pool.submit(fn, *args, **kwargs)

    def _dispatch_aggregate(
        self, fn: Callable, *args: Any, **kwargs: Any,
    ) -> Any:
        """Отправить задачу в WorkerManager."""
        self._metrics["aggregate"] += 1
        worker_manager_tasks_submitted_total.labels(status="ok").inc()
        log.debug(
            "Dispatched to worker manager",
            extra={"fn": fn.__name__},
        )
        return self._worker_manager.submit(fn, *args, **kwargs)
