"""Нагрузочные тесты — производительность MIA."""
import sys
import os
import time
from typing import Any

import pytest

# Корень проекта — в sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from application import Application
from module_base import ModuleBase, api_method
from event_bus import EventBus


# ── Вспомогательные функции ────────────────────────────────────────


def cpu_task(n: int) -> int:
    """CPU-bound задача для ProcessPool."""
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


class TestThreadPoolThroughput:
    """100 задач в ThreadPool за < 2 секунд."""

    def test_thread_pool_throughput(self):
        """100 задач отправлены через submit и завершены."""
        state = Application(modules_dir="modules")
        state.startup()

        results = []
        futures = []

        def task(x: int) -> int:
            return x * x

        start = time.time()
        for i in range(100):
            f = state.thread_pool.submit(task, i)
            futures.append(f)

        # Ждём все
        for f in futures:
            results.append(f.result(timeout=5))
        elapsed = time.time() - start

        print(f"\n100 thread tasks: {elapsed:.3f}s")
        assert len(results) == 100
        assert results == [i * i for i in range(100)]
        assert elapsed < 2.0, f"Слишком медленно: {elapsed:.3f}s"

        state.shutdown()


class TestProcessPoolThroughput:
    """50 задач в ProcessPool за < 5 секунд."""

    def test_process_pool_throughput(self):
        """50 задач отправлены в ProcessPool и завершены."""
        state = Application(modules_dir="modules")
        state.startup()

        pool = state.create_process_pool(num_processes=2)

        start = time.time()
        results = []
        for i in range(50):
            r = pool.submit(cpu_task, 100)
            results.append(r)
        elapsed = time.time() - start

        print(f"\n50 process tasks: {elapsed:.3f}s")
        assert len(results) == 50
        for r in results:
            assert r == sum(j * j for j in range(100))
        assert elapsed < 5.0, f"Слишком медленно: {elapsed:.3f}s"

        state.shutdown()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])