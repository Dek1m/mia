"""SmartDispatcher — простой маршрутизатор задач.

Все задачи идут через WorkerManager (процессы с ThreadPool внутри).
LoadBalancer выбирает наименее загруженный воркер.
"""
from __future__ import annotations

import asyncio
import inspect
import threading
from concurrent.futures import Future
from typing import Any, Callable

from argenta_logging import get_logger
from core.task import Task, TaskStatus, TaskType
from core.task_store import TaskStore
from monitoring.metrics import (
    worker_manager_tasks_submitted_total,
    task_completed_total,
    task_duration_seconds,
)

log = get_logger(__name__)


def _run_async_in_process(fn: Callable, args: tuple, kwargs: dict) -> Any:
    """Выполнить async-функцию через asyncio.run (вызывается в worker-процессе).

    Если уже есть running loop — выполняет в отдельном потоке через run_coroutine_threadsafe.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None:
        # Уже в event loop — выполняем в отдельном потоке
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, fn(*args, **kwargs))
            return future.result()
    else:
        return asyncio.run(fn(*args, **kwargs))


class SmartDispatcher:
    """Простой маршрутизатор задач.

    Новая архитектура:
      Task → SmartDispatcher → LoadBalancer → WorkerManager → WorkerThreadPool → SharedMemory → результат

    Все задачи выполняются через WorkerManager (процессы).
    """

    def __init__(
        self,
        worker_manager: Any,
        thread_pool: Any | None = None,
        task_store: TaskStore | None = None,
    ) -> None:
        self._worker_manager = worker_manager
        self._thread_pool = thread_pool
        self._write_lock = threading.Lock()
        self._task_store = task_store

        self._metrics: dict[str, int] = {
            "cpu": 0,
            "gpu": 0,
            "network": 0,
            "database": 0,
            "aggregate": 0,
            "unknown": 0,
        }

    # === Публичный API ===

    def dispatch(self, first: Any, *args: Any, **kwargs: Any) -> Any:
        """Маршрутизировать и выполнить задачу.

        Поддерживает два режима вызова:

        1. ``dispatch(fn, *args, **kwargs)`` — простая маршрутизация
        2. ``dispatch(task, fn, *args, **kwargs)`` — явный Task

        Все задачи идут через WorkerManager.
        """
        # Определяем режим вызова
        if isinstance(first, Task):
            task = first
            fn = args[0]
            call_args = args[1:]
        else:
            fn = first
            call_args = args
            task = Task.create(
                module_id=fn.__module__ if hasattr(fn, "__module__") else "unknown",
                fn_name=fn.__name__ if hasattr(fn, "__name__") else "unknown",
            )

        # Инкремент метрик
        metric_key = _task_type_to_metric_key(task.task_type)
        self._metrics[metric_key] += 1

        # Интеграция с TaskStore
        if self._task_store is not None:
            self._task_store.add(task)

        task.start()

        try:
            # Sync-задачи через ThreadPool (lambdas/methods не picklable для multiprocessing)
            worker_manager_tasks_submitted_total.labels(status="ok").inc()
            if self._thread_pool is not None:
                result = self._thread_pool.submit(fn, *call_args, **kwargs)
            else:
                result = fn(*call_args, **kwargs)
            self._complete_task(task, result)
            return result
        except Exception as exc:
            self._fail_task(task, str(exc))
            raise

    def dispatch_async(
        self, first: Any, *args: Any, **kwargs: Any,
    ) -> Future:
        """Асинхронная маршрутизация задачи.

        Все задачи идут через WorkerManager.
        Всегда возвращает Future.

        Args:
            first: Task-объект или функция для выполнения.
            *args: Аргументы функции.
            **kwargs: Именованные аргументы функции.

        Returns:
            Future с результатом выполнения.
        """
        # Определяем режим вызова
        if isinstance(first, Task):
            task = first
            fn = args[0]
            call_args = args[1:]
        else:
            fn = first
            call_args = args
            task = Task.create(
                module_id=fn.__module__ if hasattr(fn, "__module__") else "unknown",
                fn_name=fn.__name__ if hasattr(fn, "__name__") else "unknown",
            )

        # Инкремент метрик
        metric_key = _task_type_to_metric_key(task.task_type)
        self._metrics[metric_key] += 1

        # Интеграция с TaskStore
        if self._task_store is not None:
            self._task_store.add(task)

        task.start()

        # Все задачи идут через WorkerManager (процессы с ThreadPool внутри)
        if inspect.iscoroutinefunction(fn):
            pool_result = self._worker_manager.submit(
                _run_async_in_process, fn, call_args, kwargs
            )
        else:
            pool_result = self._worker_manager.submit(fn, *call_args, **kwargs)

        # Гарантируем, что возвращаем Future
        future = _ensure_future(pool_result)

        # Обработка результата для TaskStore
        self._register_task_completion(task, future)
        return future

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

    # === Внутренние методы ===

    def _complete_task(self, task: Task, result: Any) -> None:
        """Завершить задачу успешно."""
        if self._task_store is not None:
            self._task_store.complete(task, result=result)
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
        """Завершить задачу с ошибкой."""
        if self._task_store is not None:
            self._task_store.fail(task, error)
        task_completed_total.labels(
            module=task.module_id,
            task_type=task.task_type.value,
            status="failed",
        ).inc()

    def _register_task_completion(self, task: Task, pool_result: Any) -> None:
        """Зарегистрировать callback для завершения задачи когда Future готов."""
        if isinstance(pool_result, Future):
            if pool_result.done():
                self._finish_task_from_future(task, pool_result)
            else:
                pool_result.add_done_callback(
                    lambda fut: self._finish_task_from_future(task, fut)
                )
        else:
            self._complete_task(task, pool_result)

    def _finish_task_from_future(self, task: Task, fut: Future) -> None:
        """Извлечь результат из завершённого Future и завершить задачу."""
        try:
            result_value = fut.result()
            self._complete_task(task, result_value)
        except Exception as exc:
            self._fail_task(task, str(exc))


def _ensure_future(result: Any) -> Future:
    """Гарантирует, что результат обёрнут в Future.

    Если result уже Future — возвращает как есть.
    Иначе — оборачивает в已完成ленный Future.
    """
    if isinstance(result, Future):
        return result
    fut: Future = Future()
    fut.set_result(result)
    return fut


def _task_type_to_metric_key(task_type: TaskType) -> str:
    """Преобразовать TaskType → ключ метрики."""
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
