"""Общие фикстуры для тестов mia."""
from __future__ import annotations

import asyncio

import pytest

from core.task_decorator import set_global_dispatcher


@pytest.fixture(autouse=True)
def _reset_global_dispatcher() -> None:
    """Сбрасывает глобальный SmartDispatcher после каждого теста.

    Application при создании вызывает set_global_dispatcher()
    (core/application.py), что загрязняет другие тесты: @task начинает
    маршрутизировать через SmartDispatcher вместо inline-выполнения.
    Сброс после каждого теста обеспечивает изоляцию.
    """
    yield
    set_global_dispatcher(None)


@pytest.fixture(autouse=True)
def _restore_event_loop() -> None:
    """Восстанавливает event loop MainThread после каждого теста.

    asyncio.run() (Python 3.10+) вызывает set_event_loop(None) при завершении,
    что ломает последующие тесты, использующие asyncio.get_event_loop().
    Этот fixture гарантирует, что MainThread всегда имеет валидный event loop.
    """
    yield
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)