"""Общие фикстуры для тестов mia."""
from __future__ import annotations

import asyncio

import pytest

from core.task_decorator import set_global_dispatcher


@pytest.fixture(autouse=True)
def _reset_global_dispatcher() -> None:
    """После теста вернуть LocalInvokeDispatcher (не None — иначе @task падает)."""
    yield
    from core.dispatch.local import LocalInvokeDispatcher

    set_global_dispatcher(LocalInvokeDispatcher())


@pytest.fixture(autouse=True)
def _restore_event_loop() -> None:
    """Восстанавливает event loop MainThread после каждого теста."""
    yield
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
