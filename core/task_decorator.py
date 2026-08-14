"""Универсальный декоратор @task для Universal Task System."""
from __future__ import annotations

import asyncio
import functools
import time
from typing import Any, Callable, TypeVar

from argenta_logging import get_logger
from core.task import Task, TaskType
from core.errors import MiaError

log = get_logger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


class TaskValidationError(MiaError):
    """Ошибка валидации входных данных задачи."""


def task(
    type: str = "unknown",
    timeout: float = 10.0,
    retry: int = 0,
    retry_delay: float = 0.5,
    validate: type | None = None,
    audit: bool = False,
    metrics: str | None = None,
) -> Callable[[F], F]:
    """Декоратор для оборачивания функции в задачу Universal Task System.

    Устанавливает метаданные на функцию и при вызове:
    1. Валидирует входные данные (если validate)
    2. Повторяет при ошибках с exponential backoff (если retry > 0)
    3. Замеряет время выполнения
    4. Логирует вызов (если audit)
    5. Экспортирует метрики (если metrics)

    Args:
        type: Тип задачи (io, cpu, gpu, network, database, aggregate, unknown)
        timeout: Таймаут выполнения в секундах
        retry: Количество повторных попыток
        retry_delay: Базовая задержка между попытками
        validate: Pydantic модель для валидации аргументов
        audit: Включить аудит-логирование
        metrics: Имя метрики для Prometheus
    """
    task_type = TaskType(type)

    def decorator(fn: F) -> F:
        # Установка метаданных на функцию
        fn._task_type = task_type  # type: ignore[attr-defined]
        fn._task_timeout = timeout  # type: ignore[attr-defined]
        fn._task_retry = retry  # type: ignore[attr-defined]
        fn._task_retry_delay = retry_delay  # type: ignore[attr-defined]
        fn._task_validate = validate  # type: ignore[attr-defined]
        fn._task_audit = audit  # type: ignore[attr-defined]
        fn._task_metrics = metrics  # type: ignore[attr-defined]

        if asyncio.iscoroutinefunction(fn):
            return _wrap_async(fn)  # type: ignore[return-value]
        return _wrap_sync(fn)  # type: ignore[return-value]

    def _wrap_sync(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            task_obj = _create_task(fn, args, kwargs)
            _validate(task_obj, validate)
            return _execute_with_retry(
                fn, args, kwargs, task_obj, retry, retry_delay, audit, metrics
            )

        return wrapper  # type: ignore[return-value]

    def _wrap_async(fn: F) -> F:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            task_obj = _create_task(fn, args, kwargs)
            _validate(task_obj, validate)
            return await _execute_with_retry_async(
                fn, args, kwargs, task_obj, retry, retry_delay, audit, metrics
            )

        return wrapper  # type: ignore[return-value]

    return decorator


def _create_task(fn: Callable, args: tuple, kwargs: dict) -> Task:
    """Создаёт Task из аргументов вызова."""
    fn_name = getattr(fn, "_original_name", fn.__name__)
    module_id = fn.__module__ or "unknown"
    task_type = getattr(fn, "_task_type", TaskType.UNKNOWN)

    task_obj = Task.create(
        module_id=module_id,
        fn_name=fn_name,
        task_type=task_type,
        payload={"args": args, "kwargs": kwargs},
    )

    try:
        from monitoring.metrics import task_created_total
        task_created_total.labels(
            module=module_id,
            task_type=task_type.value,
        ).inc()
    except ImportError:
        pass

    return task_obj


def _validate(task_obj: Task, validate: type | None) -> None:
    """Валидирует входные данные через Pydantic схему."""
    if validate is None:
        return

    kwargs = task_obj.payload.get("kwargs", {})
    try:
        validate(**kwargs)
    except Exception as e:
        raise TaskValidationError(
            f"Validation failed for {task_obj.fn_name}: {e}"
        ) from e


def _execute_with_retry(
    fn: Callable,
    args: tuple,
    kwargs: dict,
    task_obj: Task,
    max_retries: int,
    retry_delay: float,
    audit: bool,
    metrics: str | None,
) -> Any:
    """Выполняет функцию с retry и метриками."""
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            task_obj.start()
            result = fn(*args, **kwargs)
            task_obj.complete(result)
            _log_success(task_obj, audit, metrics)
            return result
        except Exception as e:
            last_error = e
            task_obj.fail(str(e))

            if attempt < max_retries:
                delay = retry_delay * (2**attempt)
                log.warning(
                    "Task retry",
                    extra={
                        "function": task_obj.fn_name,
                        "attempt": attempt + 1,
                        "max_retries": max_retries,
                        "delay": delay,
                        "error": str(e),
                    },
                )
                time.sleep(delay)

    _log_failure(task_obj, audit, metrics)
    raise last_error  # type: ignore[misc]


async def _execute_with_retry_async(
    fn: Callable,
    args: tuple,
    kwargs: dict,
    task_obj: Task,
    max_retries: int,
    retry_delay: float,
    audit: bool,
    metrics: str | None,
) -> Any:
    """Выполняет async функцию с retry и метриками."""
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            task_obj.start()
            result = await fn(*args, **kwargs)
            task_obj.complete(result)
            _log_success(task_obj, audit, metrics)
            return result
        except Exception as e:
            last_error = e
            task_obj.fail(str(e))

            if attempt < max_retries:
                delay = retry_delay * (2**attempt)
                log.warning(
                    "Task retry",
                    extra={
                        "function": task_obj.fn_name,
                        "attempt": attempt + 1,
                        "max_retries": max_retries,
                        "delay": delay,
                        "error": str(e),
                    },
                )
                await asyncio.sleep(delay)

    _log_failure(task_obj, audit, metrics)
    raise last_error  # type: ignore[misc]


def _log_success(task_obj: Task, audit: bool, metrics: str | None) -> None:
    """Логирует успешное выполнение."""
    if audit:
        log.info(
            "Task completed",
            extra={
                "function": task_obj.fn_name,
                "duration": task_obj.duration,
                "status": "ok",
            },
        )


def _log_failure(task_obj: Task, audit: bool, metrics: str | None) -> None:
    """Логирует неуспешное выполнение."""
    if audit:
        log.error(
            "Task failed",
            extra={
                "function": task_obj.fn_name,
                "duration": task_obj.duration,
                "error": task_obj.error,
                "status": "error",
            },
        )
