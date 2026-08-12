"""Unit-тесты для ProcessPool."""
import multiprocessing
import time

import pytest

from process_pool import ProcessPool
from metrics import processpool_active


# === Топ-левел функции (для сериализации через multiprocessing) ===

def _add(x, y):
    return x + y


def _constant_42():
    return 42


def _double(x):
    return x * 2


def _return_hello():
    return "hello"


def _return_world():
    return "world"


def _identity(x):
    return x


def _merge_dicts(a, b):
    return {**a, **b}


@pytest.fixture
def pool():
    """Создаёт, запускает и останавливает пул после теста."""
    pp = ProcessPool(num_processes=2)
    pp.start()
    yield pp
    pp.shutdown(timeout=5)


# === Базовые тесты ===

def test_process_pool_creation():
    """ProcessPool() создаётся без ошибок."""
    pp = ProcessPool()
    assert pp is not None
    assert isinstance(pp, ProcessPool)
    assert pp._num_processes > 0


def test_process_pool_creation_custom():
    """ProcessPool(num_processes=4) создаётся с указанным числом процессов."""
    pp = ProcessPool(num_processes=4)
    assert pp._num_processes == 4


def test_process_pool_start():
    """start() запускает процессы — список workers не пуст."""
    pp = ProcessPool(num_processes=2)
    assert len(pp._workers) == 0
    pp.start()
    assert len(pp._workers) == 2
    assert pp._task_queue is not None
    assert pp._result_queue is not None
    pp.shutdown(timeout=5)


def test_process_pool_submit(pool):
    """submit(fn, args) возвращает результат."""
    result = pool.submit(_add, 3, 7)
    assert result == 10


def test_process_pool_submit_no_args():
    """submit(fn) без аргументов работает."""
    pp = ProcessPool(num_processes=1)
    pp.start()
    result = pp.submit(_constant_42)
    assert result == 42
    pp.shutdown(timeout=5)


# === Мультизадачность ===

def test_process_pool_multiple_tasks(pool):
    """Несколько задач выполняются и возвращают корректные результаты."""
    results = []
    for i in range(5):
        result = pool.submit(_double, i)
        results.append(result)
    assert results == [0, 2, 4, 6, 8]


def test_process_pool_sequential_tasks(pool):
    """Задачи выполняются последовательно и корректно."""
    r1 = pool.submit(_return_hello)
    r2 = pool.submit(_return_world)
    assert r1 == "hello"
    assert r2 == "world"


def test_process_pool_complex_args(pool):
    """Задача со сложными аргументами (dict)."""
    result = pool.submit(_merge_dicts, {"x": 1}, {"y": 2})
    assert result == {"x": 1, "y": 2}


# === Shutdown ===

def test_process_pool_shutdown():
    """shutdown() останавливает процессы — список workers очищается."""
    pp = ProcessPool(num_processes=2)
    pp.start()
    assert len(pp._workers) == 2
    pp.shutdown(timeout=5)
    assert len(pp._workers) == 0
    assert pp._task_queue is None


def test_process_pool_submit_after_shutdown():
    """submit после shutdown() выбрасывает RuntimeError."""
    pp = ProcessPool(num_processes=1)
    pp.start()
    pp.shutdown(timeout=5)

    with pytest.raises(RuntimeError, match="not started"):
        pp.submit(_constant_42)


# === Метрики ===

def test_process_pool_metric():
    """processpool_active обновляется при start/shutdown."""
    pp = ProcessPool(num_processes=2)
    pp.start()
    # После start() метрика должна отражать активные процессы
    active_before = processpool_active._value.get()
    assert active_before >= 2

    pp.shutdown(timeout=5)
    assert processpool_active._value.get() == 0
