"""SmartDispatcher — двухфазный маршрутизатор задач.

Фаза 1: TaskClassifier.classify() → определяет TaskType
Фаза 2: AdaptiveRouter.override() → корректирует тип при перегрузке

Обратная совместимость: если ``fn._db_type`` задан — используется legacy-логика.
"""
from __future__ import annotations

import asyncio
import inspect
import threading
from concurrent.futures import Future
from typing import Any, Callable

from argenta_logging import get_logger
from core.adaptive_router import AdaptiveRouter
from core.task import Task, TaskStatus, TaskType
from core.task_classifier import TaskClassifier
from core.task_store import TaskStore
from monitoring.metrics import (
    threadpool_tasks_submitted_total,
    worker_manager_tasks_submitted_total,
    task_completed_total,
    task_duration_seconds,
    task_adaptive_overrides_total,
)

log = get_logger(__name__)

# Legacy-маппинг: fn._db_type → TaskType
_DB_TYPE_MAP: dict[str, TaskType] = {
    "read": TaskType.IO,
    "write": TaskType.IO,
    "transaction": TaskType.DATABASE,
    "aggregate": TaskType.AGGREGATE,
}

# Типы, маршрутизируемые в ThreadPool
_THREAD_TYPES = frozenset({TaskType.IO, TaskType.CPU, TaskType.GPU, TaskType.NETWORK, TaskType.DATABASE})

# Legacy: типы, маршрутизируемые в ThreadPool (старые строковые значения)
_LEGACY_THREAD_TYPES = frozenset({"read", "write", "transaction"})


class SmartDispatcher:
    """Двухфазный маршрутизатор задач.

    Новая логика (два фаза):
      1. ``TaskClassifier.classify(task, fn)`` → ``TaskType``
      2. ``AdaptiveRouter.override(task)`` → коррекция типа при p95 > порога

    Legacy-режим (обратная совместимость):
      Если ``fn._db_type`` задан — используется прямая маршрутизация
      (read/write/transaction → ThreadPool, aggregate → WorkerManager).

    Write-lock: write-задачи с ``fn._db_lock = True`` выполняются
    последовательно через общую блокировку.
    """

    def __init__(
        self,
        thread_pool: Any,
        worker_manager: Any,
        task_store: TaskStore | None = None,
        classifier: TaskClassifier | None = None,
        adaptive_router: AdaptiveRouter | None = None,
    ) -> None:
        self._thread_pool = thread_pool
        self._worker_manager = worker_manager
        self._write_lock = threading.Lock()

        # Новые компоненты (опциональны для обратной совместимости)
        self._task_store = task_store
        self._classifier = classifier
        self._adaptive_router = adaptive_router

        self._metrics: dict[str, int] = {
            "read": 0,
            "write": 0,
            "aggregate": 0,
            "transaction": 0,
            "cpu": 0,
            "gpu": 0,
            "network": 0,
            "database": 0,
        }

    # === Публичный API ===

    def dispatch(self, first: Any, *args: Any, **kwargs: Any) -> Any:
        """Маршрутизировать и выполнить задачу.

        Поддерживает два режима вызова:

        1. ``dispatch(fn, *args, **kwargs)`` — legacy или двухфазный
        2. ``dispatch(task, fn, *args, **kwargs)`` — явный Task → двухфазный

        Режим определяется так:
          - Если передан ``Task`` → двухфазный (classify → override → dispatch)
          - Если ``fn._db_type`` задан → legacy (read/write/transaction/aggregate)
          - Иначе → двухфазный (создаёт Task внутри)
        """
        # Определяем режим вызова
        if isinstance(first, Task):
            task = first
            fn = args[0]
            call_args = args[1:]
            # Явный Task → всегда двухфазный, fn._db_type игнорируется
            return self._dispatch_two_phase(task, fn, *call_args, **kwargs)

        fn = first
        call_args = args

        # Legacy-режим: fn._db_type задан
        db_type = getattr(fn, "_db_type", None)
        if db_type is not None:
            return self._dispatch_legacy(db_type, fn, *call_args, **kwargs)

        # Новый режим: two-phase routing
        task = Task.create(
            module_id=fn.__module__ if hasattr(fn, "__module__") else "unknown",
            fn_name=fn.__name__ if hasattr(fn, "__name__") else "unknown",
        )
        return self._dispatch_two_phase(task, fn, *call_args, **kwargs)

    def dispatch_async(
        self, first: Any, *args: Any, **kwargs: Any,
    ) -> Future:
        """Асинхронная маршрутизация задачи через ThreadPool.

        Если ``fn`` — coroutine function, оборачивается в sync-обёртку
        с ``asyncio.run()`` и передаётся в ThreadPoolExecutor.
        Если ``fn`` — sync, делегирует в ``dispatch()``.

        Поддерживает два режима вызова (как dispatch):
          1. ``dispatch_async(task, fn, *args, **kwargs)`` — явный Task
          2. ``dispatch_async(fn, *args, **kwargs)`` — без Task

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
            task = None

        # Если fn — coroutine function, оборачиваем в sync-обёртку
        if inspect.iscoroutinefunction(fn):
            # Создаём Task если не передан явно
            if task is None:
                task = Task.create(
                    module_id=fn.__module__ if hasattr(fn, "__module__") else "unknown",
                    fn_name=fn.__name__ if hasattr(fn, "__name__") else "unknown",
                )

            # Классификация и override (как в _dispatch_two_phase)
            if self._classifier is not None:
                task.task_type = self._classifier.classify(task, fn)
            if self._adaptive_router is not None:
                override_type = self._adaptive_router.override(task)
                if override_type is not None:
                    task.task_type = override_type

            # Интеграция с TaskStore
            if self._task_store is not None:
                self._task_store.add(task)

            # Оборачиваем async-функцию в sync для ThreadPool.
            # Если в текущем потоке уже есть running loop (например FakeThreadPool
            # выполняет _async_wrapper inline) — запускаем в отдельном потоке,
            # чтобы не конфликтовать с существующим loop.
            # Если running loop нет (реальный ThreadPoolExecutor worker) —
            # asyncio.run() напрямую без лишних потоков.
            def _async_wrapper() -> Any:
                try:
                    running_loop = asyncio.get_running_loop()
                except RuntimeError:
                    running_loop = None

                if running_loop is not None:
                    # Running loop есть — выполняем в отдельном потоке
                    result: Any = None
                    exc_holder: list[Exception] = []

                    def _run_in_thread() -> None:
                        nonlocal result
                        try:
                            result = asyncio.run(fn(*call_args, **kwargs))
                        except Exception as exc:
                            exc_holder.append(exc)

                    thread = threading.Thread(target=_run_in_thread, daemon=True)
                    thread.start()
                    thread.join()
                    if exc_holder:
                        raise exc_holder[0]
                    return result
                else:
                    # Нет running loop — asyncio.run() напрямую
                    return asyncio.run(fn(*call_args, **kwargs))

            task.start()
            needs_lock = task.task_type == TaskType.IO and getattr(fn, "_db_lock", False)

            if needs_lock:
                with self._write_lock:
                    self._metrics["write"] += 1
                    threadpool_tasks_submitted_total.labels(status="ok").inc()
                    future = self._thread_pool.submit(_async_wrapper)
            else:
                metric_key = _task_type_to_metric_key(task.task_type)
                self._metrics[metric_key] += 1
                threadpool_tasks_submitted_total.labels(status="ok").inc()
                future = self._thread_pool.submit(_async_wrapper)

            # Обработка результата для TaskStore
            def _on_done(fut: Future) -> None:
                try:
                    result_value = fut.result()
                    if self._task_store is not None:
                        self._task_store.complete(task, result=result_value)
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
                except Exception as exc:
                    if self._task_store is not None:
                        self._task_store.fail(task, str(exc))
                    task_completed_total.labels(
                        module=task.module_id,
                        task_type=task.task_type.value,
                        status="failed",
                    ).inc()

            future.add_done_callback(_on_done)
            return future

        # sync-функция — оборачиваем в Future через thread pool
        if task is None:
            task = Task.create(
                module_id=fn.__module__ if hasattr(fn, "__module__") else "unknown",
                fn_name=fn.__name__ if hasattr(fn, "__name__") else "unknown",
            )

        # Классификация и override
        if self._classifier is not None:
            task.task_type = self._classifier.classify(task, fn)
        if self._adaptive_router is not None:
            override_type = self._adaptive_router.override(task)
            if override_type is not None:
                task.task_type = override_type

        if self._task_store is not None:
            self._task_store.add(task)

        task.start()
        needs_lock = task.task_type == TaskType.IO and getattr(fn, "_db_lock", False)

        def _sync_wrapper() -> Any:
            return fn(*call_args, **kwargs)

        if needs_lock:
            with self._write_lock:
                self._metrics["write"] += 1
                threadpool_tasks_submitted_total.labels(status="ok").inc()
                future = self._thread_pool.submit(_sync_wrapper)
        else:
            metric_key = _task_type_to_metric_key(task.task_type)
            self._metrics[metric_key] += 1
            threadpool_tasks_submitted_total.labels(status="ok").inc()
            future = self._thread_pool.submit(_sync_wrapper)

        def _on_done(fut: Future) -> None:
            try:
                result_value = fut.result()
                if self._task_store is not None:
                    self._task_store.complete(task, result=result_value)
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
            except Exception as exc:
                if self._task_store is not None:
                    self._task_store.fail(task, str(exc))
                task_completed_total.labels(
                    module=task.module_id,
                    task_type=task.task_type.value,
                    status="failed",
                ).inc()

        future.add_done_callback(_on_done)
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

    # === Новая логика: two-phase routing ===

    def _dispatch_two_phase(
        self, task: Task, fn: Callable, *args: Any, **kwargs: Any,
    ) -> Any:
        """Двухфазная маршрутизация: classify → override → dispatch."""
        # Фаза 1: классификация
        if self._classifier is not None:
            task.task_type = self._classifier.classify(task, fn)

        # Фаза 2: adaptive override
        if self._adaptive_router is not None:
            override_type = self._adaptive_router.override(task)
            if override_type is not None:
                task_adaptive_overrides_total.inc()
                log.debug(
                    "Adaptive override applied",
                    extra={"task_id": str(task.id), "from": task.task_type.value, "to": override_type.value},
                )
                task.task_type = override_type

        # Интеграция с TaskStore
        if self._task_store is not None:
            self._task_store.add(task)

        # Маршрутизация по типу
        task.start()
        try:
            if task.task_type == TaskType.AGGREGATE:
                pool_result = self._dispatch_aggregate(fn, *args, **kwargs)
            elif task.task_type in _THREAD_TYPES:
                pool_result = self._dispatch_thread_by_type(task.task_type, task, fn, *args, **kwargs)
            else:
                log.warning("Unknown task_type, fallback to thread pool", extra={"task_type": task.task_type.value})
                pool_result = self._dispatch_thread_by_type(TaskType.IO, task, fn, *args, **kwargs)

            # Извлекаем результат из Future (если доступен синхронно)
            result_value = None
            if isinstance(pool_result, Future):
                if pool_result.done():
                    try:
                        result_value = pool_result.result()
                    except Exception:
                        pass
            else:
                result_value = pool_result

            # Успех
            if self._task_store is not None:
                self._task_store.complete(task, result=result_value)
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
            return pool_result

        except Exception as exc:
            if self._task_store is not None:
                self._task_store.fail(task, str(exc))
            task_completed_total.labels(
                module=task.module_id,
                task_type=task.task_type.value,
                status="failed",
            ).inc()
            raise

    def _dispatch_thread_by_type(
        self, db_type: TaskType, task: Task, fn: Callable, *args: Any, **kwargs: Any,
    ) -> Any:
        """Отправить задачу в ThreadPool с учётом типа и write-lock."""
        needs_lock = db_type == TaskType.IO and getattr(fn, "_db_lock", False)

        if needs_lock:
            with self._write_lock:
                self._metrics["write"] += 1
                threadpool_tasks_submitted_total.labels(status="ok").inc()
                log.debug(
                    "Dispatched write (locked)",
                    extra={"fn": fn.__name__, "task_id": str(task.id)},
                )
                return self._thread_pool.submit(fn, *args, **kwargs)

        # Маппинг TaskType → legacy ключ для метрик
        metric_key = _task_type_to_metric_key(db_type)
        self._metrics[metric_key] += 1
        threadpool_tasks_submitted_total.labels(status="ok").inc()
        log.debug(
            "Dispatched to thread pool",
            extra={"fn": fn.__name__, "task_type": db_type.value, "task_id": str(task.id)},
        )
        return self._thread_pool.submit(fn, *args, **kwargs)

    # === Legacy логика (обратная совместимость) ===

    def _dispatch_legacy(
        self, db_type: str, fn: Callable, *args: Any, **kwargs: Any,
    ) -> Any:
        """Legacy-маршрутизация по ``fn._db_type`` (строковые значения)."""
        if db_type == "aggregate":
            return self._dispatch_aggregate(fn, *args, **kwargs)

        if db_type in _LEGACY_THREAD_TYPES:
            return self._dispatch_thread_legacy(db_type, fn, *args, **kwargs)

        log.warning("Unknown db_type, fallback to read", extra={"db_type": db_type})
        return self._dispatch_thread_legacy("read", fn, *args, **kwargs)

    def _dispatch_thread_legacy(
        self, db_type: str, fn: Callable, *args: Any, **kwargs: Any,
    ) -> Any:
        """Legacy: ThreadPool с write-lock для write-задач."""
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

    # === Общее ===

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


def _task_type_to_metric_key(task_type: TaskType) -> str:
    """Преобразовать TaskType → ключ метрики."""
    _MAP: dict[TaskType, str] = {
        TaskType.IO: "read",
        TaskType.CPU: "cpu",
        TaskType.GPU: "gpu",
        TaskType.NETWORK: "network",
        TaskType.DATABASE: "database",
        TaskType.AGGREGATE: "aggregate",
        TaskType.UNKNOWN: "read",
    }
    return _MAP.get(task_type, "read")
