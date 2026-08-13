"""Unit-тесты для SmartDispatcher."""
import threading
from concurrent.futures import Future

import pytest

from pools.smart_dispatcher import SmartDispatcher


# === Вспомогательные функции ===

def read_task(x):
    return x * 2


def write_task(x):
    return x + 1


def aggregate_task(x):
    return x ** 2


def transaction_task(x, y):
    return x + y


# Маркировка типов
read_task._db_type = "read"
write_task._db_type = "write"
aggregate_task._db_type = "aggregate"
transaction_task._db_type = "transaction"

# write с блокировкой
locked_write = lambda x: x * 3
locked_write._db_type = "write"
locked_write._db_lock = True


class FakeThreadPool:
    """Заглушка ThreadPool для тестов."""

    def __init__(self):
        self.submitted = []

    def submit(self, fn, *args, **kwargs):
        self.submitted.append((fn, args, kwargs))
        # Имитируем Future — возвращаем результат
        result = fn(*args, **kwargs)
        fut = Future()
        fut.set_result(result)
        return fut


class FakeWorkerManager:
    """Заглушка WorkerManager для тестов."""

    def __init__(self):
        self.submitted = []

    def submit(self, fn, *args, **kwargs):
        self.submitted.append((fn, args, kwargs))
        return fn(*args, **kwargs)


@pytest.fixture
def deps():
    tp = FakeThreadPool()
    wm = FakeWorkerManager()
    dispatcher = SmartDispatcher(tp, wm)
    return dispatcher, tp, wm


# === Тесты маршрутизации ===

def test_read_routes_to_thread_pool(deps):
    """read → ThreadPool."""
    dispatcher, tp, wm = deps
    result = dispatcher.dispatch(read_task, 5)
    assert result.result() == 10
    assert len(tp.submitted) == 1
    assert len(wm.submitted) == 0


def test_write_routes_to_thread_pool(deps):
    """write → ThreadPool."""
    dispatcher, tp, wm = deps
    result = dispatcher.dispatch(write_task, 5)
    assert result.result() == 6
    assert len(tp.submitted) == 1
    assert len(wm.submitted) == 0


def test_aggregate_routes_to_worker_manager(deps):
    """aggregate → WorkerManager."""
    dispatcher, tp, wm = deps
    result = dispatcher.dispatch(aggregate_task, 4)
    assert result == 16
    assert len(tp.submitted) == 0
    assert len(wm.submitted) == 1


def test_transaction_routes_to_thread_pool(deps):
    """transaction → ThreadPool."""
    dispatcher, tp, wm = deps
    result = dispatcher.dispatch(transaction_task, 3, 7)
    assert result.result() == 10
    assert len(tp.submitted) == 1
    assert len(wm.submitted) == 0


def test_unknown_type_fallback_to_read(deps):
    """Неизвестный тип → read (ThreadPool)."""
    dispatcher, tp, wm = deps
    fn = lambda: 42  # нет _db_type
    result = dispatcher.dispatch(fn)
    assert result.result() == 42
    assert len(tp.submitted) == 1


# === Блокировка записей ===

def test_write_with_lock_uses_lock(deps):
    """write с _db_lock=True блокируется через write_lock."""
    dispatcher, tp, wm = deps
    result = dispatcher.dispatch(locked_write, 5)
    assert result.result() == 15
    assert dispatcher.metrics["write"] == 1


def test_acquire_release_lock():
    """acquire_lock / release_lock работают."""
    tp, wm = FakeThreadPool(), FakeWorkerManager()
    dispatcher = SmartDispatcher(tp, wm)

    dispatcher.acquire_lock()
    # Второй захват должен заблокироваться в отдельном потоке
    acquired = threading.Event()

    def try_acquire():
        dispatcher.acquire_lock()
        acquired.set()
        dispatcher.release_lock()

    t = threading.Thread(target=try_acquire)
    t.start()
    assert not acquired.wait(timeout=0.1), "Lock should be held"
    dispatcher.release_lock()
    t.join(timeout=1)
    assert acquired.is_set()


# === Метрики ===

def test_metrics_counts(deps):
    """Метрики считают задачи по типам."""
    dispatcher, _, _ = deps
    dispatcher.dispatch(read_task, 1)
    dispatcher.dispatch(read_task, 2)
    dispatcher.dispatch(write_task, 3)
    dispatcher.dispatch(aggregate_task, 4)
    dispatcher.dispatch(transaction_task, 5, 6)

    m = dispatcher.metrics
    assert m["read"] == 2
    assert m["write"] == 1
    assert m["aggregate"] == 1
    assert m["transaction"] == 1


def test_metrics_is_copy(deps):
    """metrics возвращает копию, не ссылку."""
    dispatcher, _, _ = deps
    m1 = dispatcher.metrics
    dispatcher.dispatch(read_task, 1)
    m2 = dispatcher.metrics
    assert m1["read"] == 0
    assert m2["read"] == 1
