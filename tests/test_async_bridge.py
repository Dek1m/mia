"""Тесты async bridge и интеграции @task с LocalInvokeDispatcher."""
from __future__ import annotations

import asyncio
from concurrent.futures import Future
from unittest.mock import MagicMock

import pytest

from core.dispatch.local import LocalInvokeDispatcher
from core.task import Task
from core.task_decorator import _resolve_dispatcher, set_global_dispatcher, task


@pytest.fixture
def dispatcher():
    return LocalInvokeDispatcher()


class TestDispatchAsync:
    def test_async_function_runs_locally(self, dispatcher) -> None:
        async def async_fn(x: int) -> int:
            return x * 2

        future = dispatcher.dispatch_async(async_fn, 5)
        assert isinstance(future, Future)
        assert future.result() == 10

    def test_async_function_with_task_object(self, dispatcher) -> None:
        async def async_fn(x: int) -> int:
            return x + 10

        task_obj = Task.create(module_id="test", fn_name="async_fn")
        future = dispatcher.dispatch_async(task_obj, async_fn, 3)
        assert future.result() == 13

    def test_sync_function_via_dispatch_async(self, dispatcher) -> None:
        def sync_fn(x: int) -> int:
            return x * 3

        future = dispatcher.dispatch_async(sync_fn, 4)
        assert future.result() == 12


class TestTaskWithoutDispatcher:
    def setup_method(self) -> None:
        set_global_dispatcher(None)

    def test_sync_task_raises(self) -> None:
        @task(type="cpu", timeout=5.0)
        def compute(x: int) -> int:
            return x * 2

        with pytest.raises(RuntimeError, match="SmartDispatcher not initialized"):
            compute(5)

    def test_async_task_raises(self) -> None:
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


class TestTaskWithDispatcher:
    def setup_method(self) -> None:
        set_global_dispatcher(None)

    def test_sync_task_uses_dispatcher(self) -> None:
        dp = LocalInvokeDispatcher()
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
        dp = LocalInvokeDispatcher()
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


class TestGlobalDispatcher:
    def setup_method(self) -> None:
        set_global_dispatcher(None)

    def test_set_and_resolve(self) -> None:
        dp = LocalInvokeDispatcher()
        set_global_dispatcher(dp)
        assert _resolve_dispatcher() is dp

    def test_resolve_returns_none_when_not_set(self) -> None:
        assert _resolve_dispatcher() is None

    def teardown_method(self) -> None:
        set_global_dispatcher(None)
