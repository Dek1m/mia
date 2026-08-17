"""SmartDispatcher — простой маршрутизатор задач.

Маршрутизация:
  sync-функции  → ThreadPool  (blocking, для lambda/methods)
  async-функции → WorkerManager (процессы, для CPU/GPU)

Метрики: Prometheus counters (task_completed_total, task_duration_seconds).
"""
from __future__ import annotations

import asyncio
import inspect
import threading
from concurrent.futures import Future
from typing import Any, Callable

from argenta_logging import get_logger
from core.task import Task, TaskType
from monitoring.metrics import (
    worker_manager_tasks_submitted_total,
    task_completed_total,
    task_duration_seconds,
)

log = get_logger(__name__)


def _run_async_in_process(fn: Callable, args: tuple, kwargs: dict) -> Any:
    """Выполнить async-функцию через asyncio.run (вызывается в worker-процессе).

    Если уже есть running loop — выполняет в отдельном потоке.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, fn(*args, **kwargs))
            return future.result()
    else:
        return asyncio.run(fn(*args, **kwargs))


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
    """Маршрутизатор задач: sync → ThreadPool, async → WorkerManager.

    Метрики записываются напрямую в Prometheus counters.
    """

    def __init__(
        self,
        worker_manager: Any,
        thread_pool: Any | None = None,
    ) -> None:
        self._worker_manager = worker_manager
        self._thread_pool = thread_pool
        self._write_lock = threading.Lock()

    # === Публичный API ===

    def dispatch(self, first: Any, *args: Any, **kwargs: Any) -> Any:
        """Маршрутизировать и выполнить задачу (blocking).

        Поддерживает два режима:
          dispatch(fn, *args, **kwargs)
          dispatch(task, fn, *args, **kwargs)

        sync-функции  → ThreadPool  (blocking)
        async-функции → WorkerManager (blocking через .result())
        """
        return self._unified_dispatch(first, *args, **kwargs).result()

    def dispatch_async(
        self, first: Any, *args: Any, **kwargs: Any,
    ) -> Future:
        """Маршрутизировать задачу (non-blocking, возвращает Future).

        sync-функции  → ThreadPool  → Future
        async-функции → WorkerManager → Future
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
        """Единая точка маршрутизации: определяет пул и выполняет задачу.

        Возвращает Future.
        """
        task, fn, call_args = _parse_dispatch_args(first, args)

        # Prometheus: инкремент счётчика submitted
        worker_manager_tasks_submitted_total.labels(status="ok").inc()
        task.start()

        try:
            if inspect.iscoroutinefunction(fn):
                # Async → WorkerManager (процесс с asyncio.run)
                pool_result = self._worker_manager.submit(
                    _run_async_in_process, fn, call_args, kwargs,
                )
            else:
                # Sync → ThreadPool (blocking)
                if self._thread_pool is not None:
                    pool_result = self._thread_pool.submit(fn, *call_args, **kwargs)
                else:
                    pool_result = fn(*call_args, **kwargs)
        except Exception as exc:
            self._fail_task(task, str(exc))
            raise

        future = _ensure_future(pool_result)
        self._register_task_completion(task, future)
        return future

    # === Внутренние методы ===

    def _register_task_completion(self, task: Task, pool_result: Any) -> None:
        """Зарегистрировать callback для завершения задачи когда Future готов."""
        if isinstance(pool_result, Future):
            if pool_result.done():
                self._finish_task_from_future(task, pool_result)
            else:
                pool_result.add_done_callback(
                    lambda fut: self._finish_task_from_future(task, fut),
                )
        else:
            self._complete_task(task, pool_result)

    def _finish_task_from_future(self, task: Task, fut: Future) -> None:
        """Извлечь результат из завершённого Future и завершить задачу."""
        try:
            self._complete_task(task, fut.result())
        except Exception as exc:
            self._fail_task(task, str(exc))

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


def _ensure_future(result: Any) -> Future:
    """Гарантирует, что результат обёрнут в Future."""
    if isinstance(result, Future):
        return result
    fut: Future = Future()
    fut.set_result(result)
    return fut


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
