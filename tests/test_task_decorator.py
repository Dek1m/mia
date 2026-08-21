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


# ============================================================
# Экспорт в MethodRegistry: api=True → _api_meta
# ============================================================


class TestApiExport:
    """Метаданные `_api_meta`, не вызов."""

    def test_no_api_meta_by_default(self) -> None:
        @task(type="cpu")
        def plain(x: int) -> int:
            return x

        assert not hasattr(plain, "_api_meta")

    def test_api_true_sets_dict(self) -> None:
        @task(type="cpu", api=True)
        def exported(x: int) -> int:
            return x

        assert isinstance(exported._api_meta, dict)
        assert exported._api_meta["name"] == "exported"
        assert exported._api_meta["public"] is False
        assert exported._api_meta["required_permission"] is None

    def test_public_without_api_raises(self) -> None:
        with pytest.raises(ValueError, match="api=True"):
            @task(type="cpu", public=True)
            def forbidden() -> None:
                pass

    def test_permission_without_api_raises(self) -> None:
        with pytest.raises(ValueError, match="api=True"):
            @task(type="cpu", permission="llm:chat")
            def forbidden() -> None:
                pass

    def test_api_public_ok(self) -> None:
        @task(type="cpu", api=True, public=True)
        def login() -> None:
            pass

        assert login._api_meta["public"] is True

    def test_args_none_uses_task_args(self) -> None:
        @task(type="cpu", api=True)
        def compute(count: int, name: str) -> str:
            return name

        assert compute._api_meta["args"]["count"] == "int"
        assert compute._api_meta["args"]["name"] == "str"
        assert "self" not in compute._api_meta["args"]

    def test_empty_description_uses_docstring(self) -> None:
        @task(type="cpu", api=True)
        def documented() -> None:
            """Справка метода."""

        assert documented._api_meta["description"] == "Справка метода."

    def test_permission_maps_to_required_permission(self) -> None:
        @task(type="cpu", api=True, permission="llm:chat")
        def chat() -> None:
            pass

        assert chat._api_meta["required_permission"] == "llm:chat"

    def test_api_meta_has_exactly_six_keys(self) -> None:
        @task(type="cpu", api=True)
        def exported() -> None:
            pass

        assert set(exported._api_meta) == {
            "name",
            "description",
            "args",
            "return_type",
            "public",
            "required_permission",
        }

    def test_public_and_permission_together_keeps_both(self) -> None:
        @task(type="cpu", api=True, public=True, permission="llm:chat")
        def weird() -> None:
            pass

        assert weird._api_meta["public"] is True
        assert weird._api_meta["required_permission"] == "llm:chat"

    def test_method_inferred_args_omit_self(self) -> None:
        class Box:
            @task(type="cpu", api=True)
            def compute(self, count: int, name: str) -> str:
                return name

        assert Box.compute._api_meta["args"] == {"count": "int", "name": "str"}
        assert "self" not in Box.compute._api_meta["args"]

    def test_explicit_description_overrides_docstring(self) -> None:
        @task(type="cpu", api=True, description="Явное")
        def documented() -> None:
            """Докстринг."""

        assert documented._api_meta["description"] == "Явное"

    def test_missing_docstring_gives_empty_description(self) -> None:
        @task(type="cpu", api=True)
        def undocumented() -> None:
            pass

        assert undocumented._api_meta["description"] == ""

    def test_explicit_args_override_inferred(self) -> None:
        @task(type="cpu", api=True, args={"x": "number"})
        def compute(x: int) -> int:
            return x

        assert compute._api_meta["args"] == {"x": "number"}

    def test_empty_args_dict_is_not_inferred(self) -> None:
        @task(type="cpu", api=True, args={})
        def compute(x: int) -> int:
            return x

        assert compute._api_meta["args"] == {}

    def test_explicit_name_overrides_function_name(self) -> None:
        @task(type="cpu", api=True, name="login")
        def do_login() -> None:
            pass

        assert do_login._api_meta["name"] == "login"

    def test_explicit_return_type_overrides_inferred(self) -> None:
        @task(type="cpu", api=True, return_type="object")
        def compute(x: int) -> int:
            return x

        assert compute._api_meta["return_type"] == "object"

    def test_missing_return_annotation_is_none(self) -> None:
        @task(type="cpu", api=True)
        def compute(x: int):
            return x

        assert compute._api_meta["return_type"] is None

    def test_generic_annotation_keeps_type_parameters(self) -> None:
        @task(type="cpu", api=True)
        def compute(items: list[int]) -> dict[str, str]:
            return {}

        assert compute._api_meta["args"]["items"] == "list[int]"
        assert compute._api_meta["return_type"] == "dict[str, str]"

    def test_union_annotation_stringify(self) -> None:
        @task(type="cpu", api=True)
        def compute(name: str | None) -> str | None:
            return name

        assert compute._api_meta["args"]["name"] == "str | None"
        assert compute._api_meta["return_type"] == "str | None"

    def test_extract_annotations_false_leaves_args_empty(self) -> None:
        @task(type="cpu", api=True, extract_annotations=False)
        def compute(x: int) -> int:
            return x

        assert compute._api_meta["args"] == {}
        assert compute._api_meta["return_type"] is None

    def test_bound_method_exposes_api_meta(self) -> None:
        class Box:
            @task(type="cpu", api=True)
            def login(self) -> None:
                pass

        meta = getattr(Box().login, "_api_meta")
        assert meta["name"] == "login"


class TestApiExportInvariants:
    """Инварианты миграции: старый auth_method ушёл, sample @api_method жив."""

    def test_auth_decorators_py_removed(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        assert not (root / "modules" / "auth" / "decorators.py").exists()

    def test_no_legacy_auth_method_in_production_sources(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        needle = "_".join(("auth", "method"))
        extra = "_".join(("_auth", "method_meta"))
        hits: list[str] = []
        for folder in ("modules", "core", "communication", "modules_system"):
            base = root / folder
            if not base.is_dir():
                continue
            for path in base.rglob("*.py"):
                if "__pycache__" in path.parts or "tests" in path.parts:
                    continue
                text = path.read_text(encoding="utf-8")
                if needle in text or extra in text:
                    hits.append(str(path.relative_to(root)))
        assert hits == []
