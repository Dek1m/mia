"""Unit-тесты для WorkerThreadPool — пул потоков внутри воркера."""
import threading
import time

import pytest

from pools.worker_thread_pool import WorkerThreadPool


# === Фикстуры ===

@pytest.fixture
def pool():
    """Создаёт и запускает WorkerThreadPool для тестов."""
    p = WorkerThreadPool(max_threads=4)
    p.start()
    yield p
    p.shutdown(wait=True)


# === Тесты инициализации ===

def test_pool_creation():
    """WorkerThreadPool() создаётся без ошибок."""
    p = WorkerThreadPool()
    assert p is not None
    assert p.max_threads == 4


def test_pool_custom_max_threads():
    """WorkerThreadPool(max_threads=8) создаётся с正确的 размером."""
    p = WorkerThreadPool(max_threads=8)
    assert p.max_threads == 8


def test_pool_default_properties():
    """Пул имеет корректные значения по умолчанию."""
    p = WorkerThreadPool()
    assert p.workload_type == "io_bound"
    assert p.target_utilization == 0.75
    assert p.free_threads == 4
    assert p.active_count == 0


# === Тесты submit ===

def test_submit_returns_result(pool):
    """submit(fn, args) возвращает результат."""
    result = pool.submit(lambda: 42)
    assert result.result() == 42


def test_submit_with_args(pool):
    """submit(fn, *args) передаёт аргументы."""
    result = pool.submit(lambda a, b: a + b, 3, 7)
    assert result.result() == 10


def test_submit_multiple_tasks(pool):
    """Несколько submit() работают корректно."""
    futures = []
    for i in range(5):
        futures.append(pool.submit(lambda x: x * 2, i))
    results = [f.result() for f in futures]
    assert results == [0, 2, 4, 6, 8]


def test_submit_not_started_raises():
    """submit() до start() → RuntimeError."""
    p = WorkerThreadPool()
    with pytest.raises(RuntimeError, match="not started"):
        p.submit(lambda: 1)


# === Тесты free_threads ===

def test_free_threads_initial(pool):
    """free_threads == max_threads при пустом пуле."""
    assert pool.free_threads == 4


def test_free_threads_decreases_on_submit(pool):
    """free_threads уменьшается при отправке задач."""
    barrier = threading.Barrier(2)

    def slow_task():
        barrier.wait(timeout=5)
        return "done"

    # Запускаем задачу, которая будет ждать
    future = pool.submit(slow_task)
    # Даём время потоку стартовать
    time.sleep(0.05)

    # free_threads должен уменьшиться
    assert pool.free_threads <= 3
    assert pool.active_count >= 1

    # Ждём завершения
    barrier.wait(timeout=5)
    future.result(timeout=5)


def test_free_threads_recovers_after_completion(pool):
    """free_threads восстанавливается после завершения задач."""
    future = pool.submit(lambda: "done")
    future.result(timeout=5)

    # Даём время на восстановление
    time.sleep(0.05)
    assert pool.free_threads == 4
    assert pool.active_count == 0


# === Тесты shutdown ===

def test_shutdown_stops_pool():
    """shutdown() останавливает пул."""
    p = WorkerThreadPool(max_threads=2)
    p.start()
    p.shutdown(wait=True)
    assert p._executor is None


def test_shutdown_prevents_new_submits():
    """После shutdown() новые submit() отклоняются."""
    p = WorkerThreadPool(max_threads=2)
    p.start()
    p.shutdown(wait=False)
    with pytest.raises(RuntimeError, match="shutting down"):
        p.submit(lambda: 1)


def test_shutdown_is_idempotent():
    """Повторный shutdown() не падает."""
    p = WorkerThreadPool(max_threads=2)
    p.start()
    p.shutdown(wait=True)
    p.shutdown(wait=True)  # Не должно упасть


def test_shutdown_before_start():
    """shutdown() до start() не падает."""
    p = WorkerThreadPool()
    p.shutdown(wait=True)  # Не должно упасть


# === Тесты конфигурации ===

def test_pool_from_config():
    """Пул корректно загружает конфигурацию из config."""
    from core.config import MiaConfig
    MiaConfig.reset()
    try:
        p = WorkerThreadPool(max_threads=8)
        # Проверяем, что конфиг загрузился
        assert p.workload_type == "io_bound"
        assert p.target_utilization == 0.75
    finally:
        MiaConfig.reset()


def test_pool_concurrent_submit(pool):
    """Параллельные submit() работают корректно."""
    results = []

    def task(val):
        time.sleep(0.01)
        return val * 2

    futures = [pool.submit(task, i) for i in range(10)]
    results = [f.result(timeout=5) for f in futures]
    assert results == [i * 2 for i in range(10)]
