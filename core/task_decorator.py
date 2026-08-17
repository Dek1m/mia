"""Универсальный декоратор @task для Universal Task System."""
from __future__ import annotations

import asyncio
import functools
import inspect
import time
import typing
from typing import Any, Callable, TypeVar
from uuid import UUID

from argenta_logging import get_logger
from core.task import Task, TaskType
from core.errors import MiaError

log = get_logger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


class TaskValidationError(MiaError):
    """Ошибка валидации входных данных задачи."""


def _is_wrapped_async_gen(func: Any) -> bool:
    """Проверить, является ли func обёрткой над async generator.

    ``@asynccontextmanager`` оборачивает async generator в обычную функцию,
    поэтому ``inspect.isasyncgenfunction`` возвращает False.
    Проверяем через цепочку ``__wrapped__`` (устанавливается ``functools.wraps``).
    """
    seen: set[int] = set()
    current = func
    while current is not None and id(current) not in seen:
        if inspect.isasyncgenfunction(current):
            return True
        seen.add(id(current))
        current = getattr(current, "__wrapped__", None)
    return False


# Глобальный кеш dispatcher (устанавливается Application при старте)
_global_dispatcher: Any | None = None


def set_global_dispatcher(dispatcher: Any | None) -> None:
    """Установить глобальный SmartDispatcher (вызывается Application).

    Args:
        dispatcher: Экземпляр SmartDispatcher или None.
    """
    global _global_dispatcher
    _global_dispatcher = dispatcher


def _resolve_dispatcher() -> Any | None:
    """Разрешить SmartDispatcher: глобальный кеш > ServiceRegistry.

    Returns:
        SmartDispatcher если доступен, иначе None.
    """
    global _global_dispatcher
    if _global_dispatcher is not None:
        return _global_dispatcher

    try:
        from core.interfaces import ISmartDispatcher
        import sys

        # Ищем ServiceRegistry через sys.modules
        for mod_name, mod_obj in sys.modules.items():
            if not mod_name.startswith("core."):
                continue
            sr = getattr(mod_obj, "_services", None)
            if sr is not None and hasattr(sr, "has") and sr.has(ISmartDispatcher):
                _global_dispatcher = sr.resolve(ISmartDispatcher)
                log.debug("SmartDispatcher resolved from ServiceRegistry")
                return _global_dispatcher
    except Exception:
        pass

    return None


class TaskFuture:
    """Обёртка над Future с доступом к UUID задачи.

    Attributes:
        task_id: UUID задачи.
    """

    def __init__(self, future: Future, task_id: UUID) -> None:
        self._future = future
        self.task_id = task_id

    @property
    def uuid(self) -> UUID:
        """UUID задачи."""
        return self.task_id

    def result(self, timeout: float | None = None) -> Any:
        """Получить результат выполнения.

        Args:
            timeout: Максимальное время ожидания в секундах.

        Returns:
            Результат выполнения задачи.
        """
        return self._future.result(timeout=timeout)

    def done(self) -> bool:
        """Проверить, завершена ли задача."""
        return self._future.done()

    def status(self) -> str:
        """Статус задачи: 'pending' | 'running' | 'completed' | 'failed'."""
        if self._future.cancelled():
            return "cancelled"
        if self._future.done():
            try:
                self._future.result(timeout=0)
                return "completed"
            except Exception:
                return "failed"
        return "pending"

    def exception(self, timeout: float | None = None) -> Exception | None:
        """Получить исключение, если задача завершилась с ошибкой.

        Args:
            timeout: Максимальное время ожидания в секундах.
        """
        return self._future.exception(timeout=timeout)

    def __await__(self):
        """Поддержка await для async-контекстов.

        Конвертирует concurrent.futures.Future в asyncio.Future
        для корректной работы с await.
        """
        return asyncio.ensure_future(
            asyncio.wrap_future(self._future)
        ).__await__()


def task(
    type: str = "unknown",
    timeout: float | None = None,
    retry: int | None = None,
    retry_delay: float | None = None,
    validate: type | None = None,
    audit: bool = False,
    metrics: str | None = None,
    extract_annotations: bool = True,
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
        extract_angles: Извлекать type hints функции в _task_args/_task_return
    """
    task_type = TaskType(type)

    # Резолвим None-значения из конфига
    from core.config import MiaConfig
    cfg = MiaConfig.get()
    resolved_timeout = timeout if timeout is not None else cfg.get_value("core.task.timeout", 10.0)
    resolved_retry = retry if retry is not None else cfg.get_value("core.task.retry", 0)
    resolved_retry_delay = retry_delay if retry_delay is not None else cfg.get_value("core.task.retry_delay", 0.5)

    def decorator(fn: F) -> F:
        # Установка метаданных на функцию
        fn._task_type = task_type  # type: ignore[attr-defined]
        fn._task_timeout = resolved_timeout  # type: ignore[attr-defined]
        fn._task_retry = resolved_retry  # type: ignore[attr-defined]
        fn._task_retry_delay = resolved_retry_delay  # type: ignore[attr-defined]
        fn._task_validate = validate  # type: ignore[attr-defined]
        fn._task_audit = audit  # type: ignore[attr-defined]
        fn._task_metrics = metrics  # type: ignore[attr-defined]

        # Извлечение аннотаций для автоматической интроспекции
        if extract_annotations:
            try:
                hints = typing.get_type_hints(fn)
                fn._task_args = {k: v for k, v in hints.items() if k != "return"}  # type: ignore[attr-defined]
                fn._task_return = hints.get("return", None)  # type: ignore[attr-defined]
            except Exception:
                # Если get_type_hints не сработал (например, forward ref) — пропускаем
                fn._task_args = {}  # type: ignore[attr-defined]
                fn._task_return = None  # type: ignore[attr-defined]
        else:
            fn._task_args = {}  # type: ignore[attr-defined]
            fn._task_return = None  # type: ignore[attr-defined]

        if asyncio.iscoroutinefunction(fn):
            return _wrap_async(fn)  # type: ignore[return-value]
        return _wrap_sync(fn)  # type: ignore[return-value]

    def _wrap_sync(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> TaskFuture:
            task_obj = _create_task(fn, args, kwargs)
            _validate(task_obj, validate)

            dispatcher = _resolve_dispatcher()
            if dispatcher is None:
                raise RuntimeError(
                    f"SmartDispatcher not initialized. "
                    f"Cannot dispatch task '{fn.__name__}'. "
                    f"Call Application.startup() first."
                )

            last_error: Exception | None = None
            for attempt in range(resolved_retry + 1):
                try:
                    future = dispatcher.dispatch_async(task_obj, fn, *args, **kwargs)
                    return TaskFuture(future, task_obj.id)
                except Exception as e:
                    last_error = e
                    if attempt < resolved_retry:
                        delay = resolved_retry_delay * (2 ** attempt)
                        time.sleep(delay)

            raise last_error  # type: ignore[misc]

        return wrapper  # type: ignore[return-value]

    def _wrap_async(fn: F) -> F:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            task_obj = _create_task(fn, args, kwargs)
            _validate(task_obj, validate)

            dispatcher = _resolve_dispatcher()
            if dispatcher is None:
                raise RuntimeError(
                    f"SmartDispatcher not initialized. "
                    f"Cannot dispatch task '{fn.__name__}'. "
                    f"Call Application.startup() first."
                )

            last_error: Exception | None = None
            for attempt in range(resolved_retry + 1):
                try:
                    future = dispatcher.dispatch_async(task_obj, fn, *args, **kwargs)
                    loop = asyncio.get_event_loop()
                    return await asyncio.wrap_future(future, loop=loop)
                except Exception as e:
                    last_error = e
                    if attempt < resolved_retry:
                        delay = resolved_retry_delay * (2 ** attempt)
                        await asyncio.sleep(delay)

            raise last_error  # type: ignore[misc]

        # Если fn — async generator (например @asynccontextmanager),
        # @task НЕ должен оборачивать его в coroutine.
        # @asynccontextmanager оборачивает async gen в обычную функцию,
        # поэтому isasyncgenfunction(fn) → False. Проверяем через __wrapped__.
        if _is_wrapped_async_gen(fn):
            def acm_wrapper(*args: Any, **kwargs: Any) -> Any:
                return fn(*args, **kwargs)
            functools.update_wrapper(acm_wrapper, fn)
            return acm_wrapper  # type: ignore[return-value]

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
