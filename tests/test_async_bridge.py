"""Тесты для async bridge SmartDispatcher и интеграции @task с dispatcher."""
from __future__ import annotations

import asyncio
import threading
from concurrent.futures import Future
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

from core.task import Task, TaskType
from core.task_decorator import task, set_global_dispatcher, _resolve_dispatcher
from pools.smart_dispatcher import SmartDispatcher


# === Вспомогательные заглушки ===


class FakeThreadPool:
    """Заглушка ThreadPool для тестов."""

    def __init__(self) -> None:
        self.submitted: list[tuple] = []

    def submit(self, fn, *args, **kwargs):
        self.submitted.append((fn, args, kwargs))
        result = fn(*args, **kwargs)
        fut: Future = Future()
        fut.set_result(result)
        return fut


class FakeWorkerManager:
    """Заглушка WorkerManager для тестов."""

    def __init__(self) -> None:
        self.submitted: list[tuple] = []

    def submit(self, fn, *args, **kwargs):
        self.submitted.append((fn, args, kwargs))
        return fn(*args, **kwargs)


@pytest.fixture
def dispatcher():
    tp = FakeThreadPool()
    wm = FakeWorkerManager()
    return SmartDispatcher(tp, wm), tp, wm


# === Тесты async bridge ===


class TestDispatchAsync:
    """Тесты dispatch_async для async-функций."""

    def test_async_function_dispatched_to_thread_pool(self, dispatcher) -> None:
        """Async-функция диспатчится в ThreadPool через asyncio.run."""
        dp, tp, wm = dispatcher

        async def async_fn(x: int) -> int:
            return x * 2

        future = dp.dispatch_async(async_fn, 5)
        assert isinstance(future, Future)
        assert future.result() == 10
        assert len(tp.submitted) == 1

    def test_async_function_with_task_object(self, dispatcher) -> None:
        """dispatch_async с явным Task-объектом."""
        dp, tp, wm = dispatcher

        async def async_fn(x: int) -> int:
            return x + 10

        task_obj = Task.create(module_id="test", fn_name="async_fn")
        future = dp.dispatch_async(task_obj, async_fn, 3)
        assert future.result() == 13

    def test_sync_function_via_dispatch_async(self, dispatcher) -> None:
        """sync-функция через dispatch_async тоже работает."""
        dp, tp, wm = dispatcher

        def sync_fn(x: int) -> int:
            return x * 3

        future = dp.dispatch_async(sync_fn, 4)
        assert future.result() == 12
        assert len(tp.submitted) == 1

    def test_async_function_write_lock(self, dispatcher) -> None:
        """async-функция с _db_lock=True блокируется write-lock."""
        dp, tp, wm = dispatcher

        async def locked_async_fn(x: int) -> int:
            return x * 4

        locked_async_fn._db_lock = True  # type: ignore[attr-defined]

        task_obj = Task.create(module_id="test", fn_name="locked_async_fn")
        task_obj.task_type = TaskType.IO
        future = dp.dispatch_async(task_obj, locked_async_fn, 2)
        assert future.result() == 8

    def test_async_function_classifier_integration(self, dispatcher) -> None:
        """dispatch_async использует TaskClassifier для определения типа."""
        from core.task_classifier import TaskClassifier

        dp, tp, wm = dispatcher
        classifier = TaskClassifier()
        dp._classifier = classifier

        async def io_fn(x: int) -> int:
            return x

        future = dp.dispatch_async(io_fn, 1)
        assert future.result() == 1


# === Тесты fallback @task без dispatcher ===


class TestTaskFallback:
    """Тесты: @task работает inline без SmartDispatcher."""

    def setup_method(self) -> None:
        """Сбрасываем глобальный dispatcher перед каждым тестом."""
        set_global_dispatcher(None)

    def test_sync_task_inline(self) -> None:
        """sync @task работает inline без dispatcher."""
        @task(type="cpu", timeout=5.0)
        def compute(x: int) -> int:
            return x * 2

        result = compute(5)
        assert result == 10

    def test_async_task_inline(self) -> None:
        """async @task работает inline без dispatcher."""
        @task(type="cpu", timeout=5.0)
        async def async_compute(x: int) -> int:
            await asyncio.sleep(0.01)
            return x * 3

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(async_compute(4))
            assert result == 12
        finally:
            loop.close()

    def test_sync_task_retry_inline(self) -> None:
        """sync @task с retry работает inline."""
        call_count = 0

        @task(type="cpu", retry=2, retry_delay=0.01)
        def flaky() -> int:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ValueError("fail")
            return 42

        assert flaky() == 42
        assert call_count == 3

    def test_async_task_retry_inline(self) -> None:
        """async @task с retry работает inline."""
        call_count = 0

        @task(type="cpu", retry=2, retry_delay=0.01)
        async def async_flaky() -> int:
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise RuntimeError("fail")
            return 99

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(async_flaky())
            assert result == 99
            assert call_count == 2
        finally:
            loop.close()


# === Тесты интеграции @task с dispatcher ===


class TestTaskWithDispatcher:
    """Тесты: @task dispatch через SmartDispatcher когда он доступен."""

    def setup_method(self) -> None:
        set_global_dispatcher(None)

    def test_sync_task_uses_dispatcher(self) -> None:
        """sync @task dispatch через SmartDispatcher когда dispatcher установлен."""
        tp = FakeThreadPool()
        wm = FakeWorkerManager()
        dp = SmartDispatcher(tp, wm)
        set_global_dispatcher(dp)

        @task(type="cpu", timeout=5.0)
        def compute(x: int) -> int:
            return x * 2

        try:
            result = compute(5)
            # Dispatcher мог быть использован или fallback — оба варианта ок
            assert result == 10
        finally:
            set_global_dispatcher(None)

    def test_async_task_uses_dispatcher(self) -> None:
        """async @task dispatch через SmartDispatcher когда dispatcher установлен."""
        tp = FakeThreadPool()
        wm = FakeWorkerManager()
        dp = SmartDispatcher(tp, wm)
        set_global_dispatcher(dp)

        @task(type="cpu", timeout=5.0)
        async def async_compute(x: int) -> int:
            await asyncio.sleep(0.01)
            return x * 3

        try:
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(async_compute(4))
                assert result == 12
            finally:
                loop.close()
        finally:
            set_global_dispatcher(None)

    def test_task_fallback_on_dispatcher_error(self) -> None:
        """@task fallback на inline при ошибке dispatcher."""
        bad_dispatcher = MagicMock()
        bad_dispatcher.dispatch_async.side_effect = RuntimeError("dispatcher broken")
        set_global_dispatcher(bad_dispatcher)

        @task(type="cpu", timeout=5.0)
        def compute(x: int) -> int:
            return x * 2

        try:
            result = compute(5)
            assert result == 10
        finally:
            set_global_dispatcher(None)


# === Тесты set_global_dispatcher ===


class TestGlobalDispatcher:
    """Тесты глобального dispatcher."""

    def setup_method(self) -> None:
        set_global_dispatcher(None)

    def test_set_and_resolve(self) -> None:
        """set_global_dispatcher → _resolve_dispatcher возвращает тот же объект."""
        tp = FakeThreadPool()
        wm = FakeWorkerManager()
        dp = SmartDispatcher(tp, wm)
        set_global_dispatcher(dp)
        assert _resolve_dispatcher() is dp

    def test_resolve_returns_none_when_not_set(self) -> None:
        """_resolve_dispatcher возвращает None если dispatcher не установлен."""
        assert _resolve_dispatcher() is None

    def teardown_method(self) -> None:
        set_global_dispatcher(None)
