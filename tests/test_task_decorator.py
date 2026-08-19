"""Тесты для @task декоратора."""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import Future
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel

from core.task_decorator import task, TaskValidationError, set_global_dispatcher, TaskFuture
from core.task import TaskType


# ============================================================
# Тестовые модели
# ============================================================


class ComputationInput(BaseModel):
    """Схема валидации для вычислений."""

    data: list[int]
    multiplier: int = 1


# ============================================================
def _make_dispatcher() -> Any:
    """Локальный диспетчер для unit-тестов @task."""
    from core.dispatch.local import LocalInvokeDispatcher

    return LocalInvokeDispatcher()


# ============================================================
# Тестовые функции (декорируются ДО тестов)
# ============================================================


@task(type="cpu", timeout=5.0, retry=3, retry_delay=0.1)
def cpu_task(data: list[int]) -> int:
    """CPU-bound задача."""
    return sum(data) * 2


@task(type="io", timeout=2.0)
def io_task(path: str) -> str:
    """IO-bound задача."""
    return f"read {path}"


@task(type="database", retry=2, retry_delay=0.05)
def db_task(query: str) -> list[dict]:
    """Database задача."""
    return [{"result": query}]


@task(type="network", validate=ComputationInput, audit=True, metrics="network_op")
def validated_task(data: list[int], multiplier: int = 1) -> int:
    """Задача с валидацией и аудитом."""
    return sum(data) * multiplier


@task(type="cpu", retry=2, retry_delay=0.05)
def flaky_task() -> int:
    """Задача, которая падает первые 2 раза."""
    flaky_task.call_count += 1  # type: ignore[attr-defined]
    if flaky_task.call_count < 3:  # type: ignore[attr-defined]
        raise ValueError(f"Attempt {flaky_task.call_count} failed")
    return 42


@task(type="cpu")
async def async_task(x: int) -> int:
    """Async задача."""
    await asyncio.sleep(0.01)
    return x * 3


@task(type="cpu", retry=2, retry_delay=0.05)
async def async_flaky_task() -> int:
    """Async задача, которая падает первый раз."""
    async_flaky_task.call_count += 1  # type: ignore[attr-defined]
    if async_flaky_task.call_count < 2:  # type: ignore[attr-defined]
        raise RuntimeError("First attempt failed")
    return 99


# ============================================================
# Тесты метаданных
# ============================================================


class TestMetadata:
    """Проверка установки метаданных на функцию."""

    def test_task_type(self):
        assert cpu_task._task_type == TaskType.CPU
        assert io_task._task_type == TaskType.IO
        assert db_task._task_type == TaskType.DATABASE
        assert validated_task._task_type == TaskType.NETWORK

    def test_timeout(self):
        assert cpu_task._task_timeout == 5.0
        assert io_task._task_timeout == 2.0

    def test_retry(self):
        assert cpu_task._task_retry == 3
        assert io_task._task_retry == 0
        assert db_task._task_retry == 2

    def test_retry_delay(self):
        assert cpu_task._task_retry_delay == 0.1
        assert db_task._task_retry_delay == 0.05

    def test_validate(self):
        assert cpu_task._task_validate is None
        assert validated_task._task_validate == ComputationInput

    def test_audit(self):
        assert cpu_task._task_audit is False
        assert validated_task._task_audit is True

    def test_metrics(self):
        assert cpu_task._task_metrics is None
        assert validated_task._task_metrics == "network_op"


# ============================================================
# Тесты выполнения (через mock dispatcher)
# ============================================================


class TestExecution:
    """Проверка базового выполнения задач через SmartDispatcher."""

    def setup_method(self) -> None:
        self._dispatcher = _make_dispatcher()
        set_global_dispatcher(self._dispatcher)

    def teardown_method(self) -> None:
        set_global_dispatcher(None)

    def test_sync_execution(self):
        future = cpu_task([1, 2, 3])
        assert isinstance(future, TaskFuture)
        assert future.uuid is not None
        assert future.result() == 12  # (1+2+3) * 2

    def test_sync_execution_simple(self):
        future = io_task("test.txt")
        assert isinstance(future, TaskFuture)
        assert future.result() == "read test.txt"

    def test_db_task(self):
        future = db_task("SELECT *")
        assert isinstance(future, TaskFuture)
        assert future.result() == [{"result": "SELECT *"}]

    @pytest.mark.asyncio
    async def test_async_execution(self):
        result = await async_task(5)
        assert result == 15  # 5 * 3

    def test_task_future_has_uuid(self):
        """TaskFuture содержит UUID задачи."""
        future = cpu_task([1, 2, 3])
        assert hasattr(future, "uuid")
        assert future.uuid is not None
        assert future.task_id == future.uuid

    def test_task_future_status(self):
        """TaskFuture.status() возвращает корректный статус."""
        future = cpu_task([1, 2, 3])
        assert future.status() == "completed"
        assert future.done() is True


# ============================================================
# Тесты retry (через mock dispatcher)
# ============================================================


class TestRetry:
    """Проверка retry логики через SmartDispatcher."""

    def setup_method(self) -> None:
        self._dispatcher = _make_dispatcher()
        set_global_dispatcher(self._dispatcher)

    def teardown_method(self) -> None:
        set_global_dispatcher(None)

    def test_retry_metadata_set(self):
        """@task(retry=3) устанавливает retry метаданные."""
        assert cpu_task._task_retry == 3

    def test_retry_delay_metadata_set(self):
        """@task(retry_delay=0.1) устанавливает retry_delay метаданные."""
        assert cpu_task._task_retry_delay == 0.1

    def test_retry_zero_metadata(self):
        """@task(retry=0) устанавливает retry=0."""
        assert io_task._task_retry == 0

    def test_error_propagates(self):
        """Ошибка прокидывается через SmartDispatcher (retry на уровне декоратора удалён)."""
        @task(type="cpu", retry=2, retry_delay=0.01)
        def always_fail() -> int:
            raise ValueError("Always fails")

        with pytest.raises(ValueError, match="Always fails"):
            always_fail().result()


# ============================================================
# Тесты валидации (через mock dispatcher)
# ============================================================


class TestValidation:
    """Проверка валидации через Pydantic."""

    def setup_method(self) -> None:
        self._dispatcher = _make_dispatcher()
        set_global_dispatcher(self._dispatcher)

    def teardown_method(self) -> None:
        set_global_dispatcher(None)

    def test_validation_success(self):
        future = validated_task(data=[1, 2, 3], multiplier=5)
        assert isinstance(future, TaskFuture)
        assert future.result() == 30  # (1+2+3) * 5

    def test_validation_failure(self):
        with pytest.raises(TaskValidationError):
            validated_task(data="not a list")  # type: ignore[arg-type]

    def test_validation_with_invalid_kwarg(self):
        with pytest.raises(TaskValidationError):
            validated_task(data=[1, 2], multiplier="bad")  # type: ignore[arg-type]


# ============================================================
# Тесты аудита (через mock dispatcher)
# ============================================================


class TestAudit:
    """Проверка аудит-метаданных (аудит-логирование теперь на уровне SmartDispatcher)."""

    def test_audit_metadata_set(self):
        """@task(audit=True) устанавливает audit метаданные."""
        assert validated_task._task_audit is True

    def test_audit_false_by_default(self):
        """По умолчанию audit=False."""
        assert cpu_task._task_audit is False

    def test_metrics_metadata_set(self):
        """@task(metrics=...) устанавливает metrics метаданные."""
        assert validated_task._task_metrics == "network_op"


# ============================================================
# Тесты functools.wraps
# ============================================================


class TestWraps:
    """Проверка сохранения метаданных функции."""

    def test_preserves_name(self):
        assert cpu_task.__name__ == "cpu_task"

    def test_preserves_docstring(self):
        assert cpu_task.__doc__ == "CPU-bound задача."

    def test_preserves_module(self):
        assert cpu_task.__module__ == __name__


# ============================================================
# Тесты без dispatcher
# ============================================================


class TestNoDispatcher:
    """Проверка: без SmartDispatcher — RuntimeError."""

    def setup_method(self) -> None:
        set_global_dispatcher(None)

    def test_sync_task_raises(self):
        @task(type="cpu")
        def compute(x: int) -> int:
            return x * 2

        with pytest.raises(RuntimeError, match="SmartDispatcher not initialized"):
            compute(5)

    def test_async_task_raises(self):
        @task(type="cpu")
        async def async_compute(x: int) -> int:
            return x * 3

        with pytest.raises(RuntimeError, match="SmartDispatcher not initialized"):
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(async_compute(5))
            finally:
                loop.close()
