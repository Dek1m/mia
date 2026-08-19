"""Нагрузочные тесты — производительность MIA."""
import sys
import os
import time
from typing import Any

import pytest

# Корень проекта — в sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.application import Application
from modules_system.module_base import ModuleBase, api_method
from communication.event_bus import EventBus


# ── Вспомогательные функции ────────────────────────────────────────


def cpu_task(n: int) -> int:
    """CPU-bound задача для локального диспетчера."""
    return sum(i * i for i in range(n))


class PerfModule(ModuleBase):
    """Модуль для тестов производительности."""

    @property
    def name(self) -> str:
        return "perf"

    @api_method
    def fast(self, x: int) -> int:
        return x * 2

    @api_method
    def slow(self, x: int) -> int:
        time.sleep(0.001)
        return x * 3


# ── Тесты ──────────────────────────────────────────────────────────


class TestManyApiCalls:
    """1000 вызовов API за < 1 секунды."""

    def test_many_api_calls(self):
        """1000 последовательных API вызовов."""
        state = Application(modules_dir="modules")
        state.startup()
        state.load_module("sample")

        start = time.time()
        for i in range(1000):
            state.api.sample.add(i, i)
        elapsed = time.time() - start

        print(f"\n1000 API calls: {elapsed:.3f}s")
        assert elapsed < 1.0, f"Слишком медленно: {elapsed:.3f}s"

        state.shutdown()


class TestManyEvents:
    """100 публикаций EventBus за < 0.5 секунд."""

    def test_many_events(self):
        """100 публикаций с подписчиком."""
        state = Application(modules_dir="modules")
        state.startup()

        received = []

        def handler(data: Any) -> None:
            received.append(data)

        state.event_bus.subscribe("load.test", handler)

        start = time.time()
        for i in range(100):
            state.event_bus.publish("load.test", {"i": i})
        elapsed = time.time() - start

        print(f"\n100 events: {elapsed:.3f}s")
        assert len(received) == 100
        assert elapsed < 0.5, f"Слишком медленно: {elapsed:.3f}s"

        state.shutdown()


class TestLocalDispatchThroughput:
    """50 локальных задач за < 5 секунд."""

    def test_local_dispatch_throughput(self):
        from core.dispatch.local import LocalInvokeDispatcher

        dp = LocalInvokeDispatcher()
        start = time.time()
        results = [dp.dispatch(cpu_task, 100) for _ in range(50)]
        elapsed = time.time() - start
        assert len(results) == 50
        assert all(r == sum(j * j for j in range(100)) for r in results)
        assert elapsed < 5.0, f"Слишком медленно: {elapsed:.3f}s"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])