"""SmartDispatcher — простой маршрутизатор задач через SharedMemory.

Маршрутизация:
  Все функции → SharedMemory (очередь + хранилище результатов)

Метрики: Prometheus counters (task_completed_total, task_duration_seconds).
"""
from __future__ import annotations

import asyncio
import inspect
import pickle
import threading
import time
from concurrent.futures import Future
from typing import Any, Callable

from argenta_logging import get_logger
from core.shared_memory import SharedMemory, TaskData
from core.task import Task, TaskType
from monitoring.metrics import (
    worker_manager_tasks_submitted_total,
    task_completed_total,
    task_duration_seconds,
)

log = get_logger(__name__)


def _run_async_sync(fn: Callable, args: tuple, kwargs: dict) -> Any:
    """Выполнить async-функцию синхронно.

    Если event loop уже запущен — выполняет в отдельном потоке с новым loop.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # Нет running loop — можно использовать asyncio.run
        return asyncio.run(fn(*args, **kwargs))

    # Есть running loop — выполняем в отдельном потоке
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(asyncio.run, fn(*args, **kwargs))
        return future.result()


def _parse_dispatch_args(first: Any, args: tuple) -> tuple[Task, Callable, tuple]:
    """Извлечь Task, fn и call_args из аргументов dispatch.

    Поддерживает два режима:
      dispatch(fn, *args)         → (auto_task, fn, args)
      dispatch(task, fn, *args)  → (task, fn, args)
    """
    if isinstance(first, Task):
        return first, args[0], args[1:]
    return (
        Task.create(
            module_id=getattr(first, "__module__", "unknown"),
            fn_name=getattr(first, "__name__", "unknown"),
        ),
        first,
        args,
    )


class SmartDispatcher:
    """Маршрутизатор задач через SharedMemory.

    Все задачи dispatch'ятся через SharedMemory.
    Результаты хранятся в SharedMemory.
    """

    def __init__(
        self,
        worker_manager: Any,
        shared_memory: SharedMemory | None = None,
    ) -> None:
        self._worker_manager = worker_manager
        self._shared_memory = shared_memory
        self._write_lock = threading.Lock()

    # === Публичный API ===

    def dispatch(self, first: Any, *args: Any, **kwargs: Any) -> Any:
        """Маршрутизировать и выполнить задачу (blocking).

        Поддерживает два режима:
          dispatch(fn, *args, **kwargs)
          dispatch(task, fn, *args, **kwargs)

        Все задачи → SharedMemory.
        """
        future = self._unified_dispatch(first, *args, **kwargs)
        return future.result()

    def dispatch_async(
        self, first: Any, *args: Any, **kwargs: Any,
    ) -> Future:
        """Маршрутизировать задачу (non-blocking, возвращает Future).

        Все задачи → SharedMemory.
        """
        return self._unified_dispatch(first, *args, **kwargs)

    def acquire_lock(self) -> None:
        """Захватить блокировку записей."""
        self._write_lock.acquire()

    def release_lock(self) -> None:
        """Освободить блокировку записей."""
        self._write_lock.release()

    # === Ядро маршрутизации ===

    def _unified_dispatch(
        self, first: Any, *args: Any, **kwargs: Any,
    ) -> Future:
        """Единая точка маршрутизации: создаёт TaskData и выполняет через SharedMemory.

        Возвращает Future с результатом.
        """
        task, fn, call_args = _parse_dispatch_args(first, args)

        # Prometheus: инкремент счётчика submitted
        worker_manager_tasks_submitted_total.labels(status="ok").inc()
        task.start()

        # Создаём TaskData (сериализуем args/kwargs если возможно)
        try:
            args_serialized = pickle.dumps(call_args)
            kwargs_serialized = pickle.dumps(kwargs)
        except Exception:
            # Локальные функции/моки не сериализуются — передаём пустые байты
            args_serialized = b""
            kwargs_serialized = b""

        task_data = TaskData(
            uuid=str(task.id),
            function_name=fn.__name__,
            module_name=getattr(fn, "__module__", "unknown"),
            args_serialized=args_serialized,
            kwargs_serialized=kwargs_serialized,
            created_at=time.time(),
            priority=task.priority,
        )

        # Отправляем в SharedMemory (если доступна)
        if self._shared_memory is not None:
            self._shared_memory.submit_task(task_data)

        # Выполняем функцию
        try:
            if inspect.iscoroutinefunction(fn):
                result = _run_async_sync(fn, call_args, kwargs)
            else:
                result = fn(*call_args, **kwargs)
        except Exception as exc:
            self._fail_task(task, str(exc))
            raise

        # Сохраняем результат в SharedMemory (если доступна)
        if self._shared_memory is not None:
            try:
                self._shared_memory.store_result(task.id, result)
            except Exception:
                pass  # Результат может не сериализоваться

        self._complete_task(task, result)
        fut = Future()
        fut.set_result(result)
        return fut

    # === Внутренние методы ===

    def _complete_task(self, task: Task, result: Any) -> None:
        """Завершить задачу успешно. Метрики → Prometheus."""
        task_completed_total.labels(
            module=task.module_id,
            task_type=task.task_type.value,
            status="completed",
        ).inc()
        if task.duration is not None:
            task_duration_seconds.labels(
                module=task.module_id,
                task_type=task.task_type.value,
            ).observe(task.duration)

    def _fail_task(self, task: Task, error: str) -> None:
        """Завершить задачу с ошибкой. Метрики → Prometheus."""
        task_completed_total.labels(
            module=task.module_id,
            task_type=task.task_type.value,
            status="failed",
        ).inc()


def _task_type_to_metric_key(task_type: TaskType) -> str:
    """Преобразовать TaskType → ключ метрики (для обратной совместимости)."""
    _MAP: dict[TaskType, str] = {
        TaskType.IO: "unknown",
        TaskType.CPU: "cpu",
        TaskType.GPU: "gpu",
        TaskType.NETWORK: "network",
        TaskType.DATABASE: "database",
        TaskType.AGGREGATE: "aggregate",
        TaskType.UNKNOWN: "unknown",
    }
    return _MAP.get(task_type, "unknown")
