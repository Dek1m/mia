"""Тесты для async bridge SmartDispatcher и интеграции @task с dispatcher."""
from __future__ import annotations

import asyncio
from concurrent.futures import Future
from unittest.mock import MagicMock

import pytest

from core.task import Task, TaskType
from core.task_decorator import task, set_global_dispatcher, _resolve_dispatcher
from pools.smart_dispatcher import SmartDispatcher


# === Вспомогательные заглушки ===


class FakeWorkerManager:
    """Заглушка WorkerManager для тестов."""

    def __init__(self) -> None:
        self.submitted: list[tuple] = []

    def submit(self, fn, *args, **kwargs):
        self.submitted.append((fn, args, kwargs))
        return fn(*args, **kwargs)


class FakeThreadPool:
    """Заглушка ThreadPool для тестов."""

    def __init__(self) -> None:
        self.submitted: list[tuple] = []

    def submit(self, fn, *args, **kwargs):
        self.submitted.append((fn, args, kwargs))
        return fn(*args, **kwargs)

    def start(self) -> None:
        pass

    def shutdown(self, wait: bool = True) -> None:
        pass


@pytest.fixture
def dispatcher():
    wm = FakeWorkerManager()
    tp = FakeThreadPool()
    return SmartDispatcher(wm, thread_pool=tp), wm, tp


# === Тесты async bridge ===


class TestDispatchAsync:
    """Тесты dispatch_async для async-функций."""

    def test_async_function_dispatched_via_worker_manager(self, dispatcher) -> None:
        """Async-функция диспатчится через WorkerManager."""
        dp, wm, tp = dispatcher

        async def async_fn(x: int) -> int:
            return x * 2

        future = dp.dispatch_async(async_fn, 5)
        assert isinstance(future, Future)
        assert future.result() == 10
        assert len(wm.submitted) == 1

    def test_async_function_with_task_object(self, dispatcher) -> None:
        """dispatch_async с явным Task-объектом."""
        dp, wm, tp = dispatcher

        async def async_fn(x: int) -> int:
            return x + 10

        task_obj = Task.create(module_id="test", fn_name="async_fn")
        future = dp.dispatch_async(task_obj, async_fn, 3)
        assert future.result() == 13

    def test_sync_function_via_dispatch_async(self, dispatcher) -> None:
        """sync-функция через dispatch_async идёт через ThreadPool."""
        dp, wm, tp = dispatcher

        def sync_fn(x: int) -> int:
            return x * 3

        future = dp.dispatch_async(sync_fn, 4)
        assert future.result() == 12
        assert len(tp.submitted) == 1


# === Тесты @task без dispatcher ===


class TestTaskWithoutDispatcher:
    """Тесты: @task без SmartDispatcher выбрасывает RuntimeError."""

    def setup_method(self) -> None:
        set_global_dispatcher(None)

    def test_sync_task_raises(self) -> None:
        """sync @task без dispatcher → RuntimeError."""
        @task(type="cpu", timeout=5.0)
        def compute(x: int) -> int:
            return x * 2

        with pytest.raises(RuntimeError, match="SmartDispatcher not initialized"):
            compute(5)

    def test_async_task_raises(self) -> None:
        """async @task без dispatcher → RuntimeError."""
        @task(type="cpu", timeout=5.0)
        async def async_compute(x: int) -> int:
            await asyncio.sleep(0.01)
            return x * 3

        loop = asyncio.new_event_loop()
        try:
            with pytest.raises(RuntimeError, match="SmartDispatcher not initialized"):
                loop.run_until_complete(async_compute(4))
        finally:
            loop.close()


# === Тесты интеграции @task с dispatcher ===


class TestTaskWithDispatcher:
    """Тесты: @task dispatch через SmartDispatcher когда он доступен."""

    def setup_method(self) -> None:
        set_global_dispatcher(None)

    def test_sync_task_uses_dispatcher(self) -> None:
        """sync @task dispatch через SmartDispatcher когда dispatcher установлен."""
        wm = FakeWorkerManager()
        tp = FakeThreadPool()
        dp = SmartDispatcher(wm, thread_pool=tp)
        set_global_dispatcher(dp)

        @task(type="cpu", timeout=5.0)
        def compute(x: int) -> int:
            return x * 2

        try:
            future = compute(5)
            assert future.result() == 10
        finally:
            set_global_dispatcher(None)

    def test_async_task_uses_dispatcher(self) -> None:
        """async @task dispatch через SmartDispatcher когда dispatcher установлен."""
        wm = FakeWorkerManager()
        tp = FakeThreadPool()
        dp = SmartDispatcher(wm, thread_pool=tp)
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

    def test_task_propagates_dispatcher_error(self) -> None:
        """@task пробрасывает ошибку dispatcher."""
        bad_dispatcher = MagicMock()
        bad_dispatcher.dispatch_async.side_effect = RuntimeError("dispatcher broken")
        set_global_dispatcher(bad_dispatcher)

        @task(type="cpu", timeout=5.0)
        def compute(x: int) -> int:
            return x * 2

        try:
            with pytest.raises(RuntimeError, match="dispatcher broken"):
                compute(5)
        finally:
            set_global_dispatcher(None)


# === Тесты set_global_dispatcher ===


class TestGlobalDispatcher:
    """Тесты глобального dispatcher."""

    def setup_method(self) -> None:
        set_global_dispatcher(None)

    def test_set_and_resolve(self) -> None:
        """set_global_dispatcher → _resolve_dispatcher возвращает тот же объект."""
        wm = FakeWorkerManager()
        dp = SmartDispatcher(wm)
        set_global_dispatcher(dp)
        assert _resolve_dispatcher() is dp

    def test_resolve_returns_none_when_not_set(self) -> None:
        """_resolve_dispatcher возвращает None если dispatcher не установлен."""
        assert _resolve_dispatcher() is None

    def teardown_method(self) -> None:
        set_global_dispatcher(None)
