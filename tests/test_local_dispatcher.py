"""Unit-тесты LocalInvokeDispatcher."""
from __future__ import annotations

import asyncio

from core.dispatch.local import LocalInvokeDispatcher
from core.task import Task


def test_sync_dispatch():
    dp = LocalInvokeDispatcher()

    def add(a: int, b: int) -> int:
        return a + b

    assert dp.dispatch(add, 2, 3) == 5


def test_async_dispatch():
    dp = LocalInvokeDispatcher()

    async def mul(a: int, b: int) -> int:
        await asyncio.sleep(0)
        return a * b

    assert dp.dispatch(mul, 4, 5) == 20


def test_dispatch_async_returns_future():
    dp = LocalInvokeDispatcher()
    future = dp.dispatch_async(lambda x: x + 1, 9)
    assert future.done()
    assert future.result() == 10


def test_dispatch_with_task_object():
    dp = LocalInvokeDispatcher()
    task = Task.create(module_id="t", fn_name="inc")
    assert dp.dispatch(task, lambda x: x + 1, 1) == 2


def test_exception_propagates():
    dp = LocalInvokeDispatcher()

    def boom() -> None:
        raise ValueError("nope")

    future = dp.dispatch_async(boom)
    assert future.exception() is not None
