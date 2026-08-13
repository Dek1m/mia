"""Тесты для @task декоратора."""
from __future__ import annotations

import asyncio
import logging
import time
from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel, ValidationError

from core.task_decorator import task, TaskValidationError
from core.task import TaskType


# ============================================================
# Тестовые модели
# ============================================================


class ComputationInput(BaseModel):
    """Схема валидации для вычислений."""

    data: list[int]
    multiplier: int = 1


# ============================================================
# Тестовые функции
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
# Тесты выполнения
# ============================================================


class TestExecution:
    """Проверка базового выполнения задач."""

    def test_sync_execution(self):
        result = cpu_task([1, 2, 3])
        assert result == 12  # (1+2+3) * 2

    def test_sync_execution_simple(self):
        result = io_task("test.txt")
        assert result == "read test.txt"

    def test_db_task(self):
        result = db_task("SELECT *")
        assert result == [{"result": "SELECT *"}]

    @pytest.mark.asyncio
    async def test_async_execution(self):
        result = await async_task(5)
        assert result == 15  # 5 * 3


# ============================================================
# Тесты retry
# ============================================================


class TestRetry:
    """Проверка retry логики."""

    def test_retry_success_after_failures(self):
        flaky_task.call_count = 0  # type: ignore[attr-defined]
        result = flaky_task()
        assert result == 42
        assert flaky_task.call_count == 3  # type: ignore[attr-defined]

    def test_retry_exhausted_raises(self):
        @task(type="cpu", retry=1, retry_delay=0.01)
        def always_fail() -> int:
            always_fail.call_count += 1  # type: ignore[attr-defined]
            raise ValueError("Always fails")

        always_fail.call_count = 0  # type: ignore[attr-defined]
        with pytest.raises(ValueError, match="Always fails"):
            always_fail()
        assert always_fail.call_count == 2  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_async_retry_success(self):
        async_flaky_task.call_count = 0  # type: ignore[attr-defined]
        result = await async_flaky_task()
        assert result == 99
        assert async_flaky_task.call_count == 2  # type: ignore[attr-defined]


# ============================================================
# Тесты валидации
# ============================================================


class TestValidation:
    """Проверка валидации через Pydantic."""

    def test_validation_success(self):
        result = validated_task(data=[1, 2, 3], multiplier=5)
        assert result == 30  # (1+2+3) * 5

    def test_validation_failure(self):
        with pytest.raises(TaskValidationError):
            validated_task(data="not a list")  # type: ignore[arg-type]

    def test_validation_with_invalid_kwarg(self):
        with pytest.raises(TaskValidationError):
            validated_task(data=[1, 2], multiplier="bad")  # type: ignore[arg-type]


# ============================================================
# Тесты аудита
# ============================================================


class TestAudit:
    """Проверка аудит-логирования."""

    def test_audit_success(self, caplog):
        with caplog.at_level(logging.INFO):
            validated_task(data=[1, 2])

        assert "Task completed" in caplog.text
        assert "network_op" not in caplog.text  # metrics, не в логах

    def test_audit_failure(self, caplog):
        @task(type="cpu", audit=True, retry=0)
        def failing() -> None:
            raise RuntimeError("boom")

        with caplog.at_level(logging.ERROR):
            with pytest.raises(RuntimeError):
                failing()

        assert "Task failed" in caplog.text


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
