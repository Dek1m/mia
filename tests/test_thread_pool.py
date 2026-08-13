"""Unit-тесты для ThreadPoolManager."""
import time
import threading
from concurrent.futures import Future

import pytest

from pools.thread_pool import ThreadPoolManager
from monitoring.metrics import threadpool_active


@pytest.fixture
def pool():
    """Создаёт и запускает пул, после теста — останавливает."""
    tp = ThreadPoolManager(max_workers=2)
    tp.start()
    yield tp
    tp.shutdown(wait=True)


# === Базовые тесты ===

def test_thread_pool_creation():
    """ThreadPoolManager() создаётся без ошибок."""
    tp = ThreadPoolManager()
    assert tp is not None
    assert isinstance(tp, ThreadPoolManager)
    assert tp._executor is None  # ещё не запущен


def test_thread_pool_start():
    """start() запускает пул — executor создаётся."""
    tp = ThreadPoolManager(max_workers=2)
    assert tp._executor is None
    tp.start()
    assert tp._executor is not None
    tp.shutdown(wait=True)


def test_thread_pool_submit(pool):
    """submit(fn) возвращает Future."""
    future = pool.submit(lambda: 42)
    assert isinstance(future, Future)


def test_thread_pool_submit_result(pool):
    """submit(fn, args).result() возвращает корректный результат."""
    future = pool.submit(lambda x, y: x + y, 3, 7)
    assert future.result(timeout=5) == 10


def test_thread_pool_shutdown():
    """shutdown() останавливает пул — executor становится None."""
    tp = ThreadPoolManager(max_workers=1)
    tp.start()
    assert tp._executor is not None
    tp.shutdown(wait=True)
    assert tp._executor is None


def test_thread_pool_shutdown_wait():
    """shutdown(wait=True) дожидается завершения всех задач."""
    results = []

    def slow_task():
        time.sleep(0.1)
        results.append("done")

    tp = ThreadPoolManager(max_workers=2)
    tp.start()
    tp.submit(slow_task)
    tp.submit(slow_task)
    tp.shutdown(wait=True)

    # После wait=True обе задачи гарантированно завершились
    assert results.count("done") == 2


def test_thread_pool_submit_after_shutdown():
    """submit после shutdown() выбрасывает RuntimeError."""
    tp = ThreadPoolManager(max_workers=1)
    tp.start()
    tp.shutdown(wait=True)

    with pytest.raises(RuntimeError, match="ThreadPool not started"):
        tp.submit(lambda: 1)


# === Мультизадачность ===

def test_thread_pool_multiple_tasks():
    """Несколько задач выполняются и возвращают корректные результаты."""
    tp = ThreadPoolManager(max_workers=4)
    tp.start()

    futures = [tp.submit(lambda i=i: i * 2) for i in range(10)]
    results = [f.result(timeout=5) for f in futures]
    assert results == [i * 2 for i in range(10)]

    tp.shutdown(wait=True)


def test_thread_pool_concurrent():
    """Задачи выполняются параллельно, а не последовательно."""
    tp = ThreadPoolManager(max_workers=2)
    tp.start()

    # 4 задачи по 0.1с каждая. При 2 потоках — ~0.2с, при последовательном — ~0.4с
    start = time.monotonic()
    futures = [tp.submit(lambda: time.sleep(0.1)) for _ in range(4)]
    for f in futures:
        f.result(timeout=5)
    elapsed = time.monotonic() - start

    # Допускаем запас на overhead, но явно меньше чем 0.4с
    assert elapsed < 0.35, f"Expected parallel execution < 0.35s, got {elapsed:.3f}s"

    tp.shutdown(wait=True)


# === Метрики ===

def test_thread_pool_metric():
    """threadpool_active обновляется при start/shutdown."""
    tp = ThreadPoolManager(max_workers=3)

    # До start — метрика может быть любым значением (глобальная)
    tp.start()
    assert threadpool_active._value.get() == 3

    tp.shutdown(wait=True)
    assert threadpool_active._value.get() == 0
