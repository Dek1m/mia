"""Unit-тесты для LoadBalancer — балансировщик нагрузки."""
import pytest
from pools.load_balancer import LoadBalancer, WorkerState


# === Фикстуры ===

@pytest.fixture
def lb():
    """Создаёт LoadBalancer для тестов."""
    return LoadBalancer()


def _make_state(
    worker_id: int = 0,
    cpu_load: float = 0.0,
    active_tasks: int = 0,
    stale_penalty: float = 0.0,
    pid: int = 1000,
    core_id: int = 0,
    free_threads: int = 4,
    max_threads: int = 4,
) -> WorkerState:
    """Хелпер для создания WorkerState."""
    return WorkerState(
        worker_id=worker_id,
        pid=pid,
        cpu_load=cpu_load,
        active_tasks=active_tasks,
        stale_penalty=stale_penalty,
        core_id=core_id,
        free_threads=free_threads,
        max_threads=max_threads,
    )


# === Тесты select_worker ===

def test_select_worker_empty(lb):
    """Пустой dict → None."""
    result = lb.select_worker({})
    assert result is None


def test_select_worker_single(lb):
    """Один воркер → его ID."""
    workers = {42: _make_state(worker_id=42, cpu_load=0.5)}
    result = lb.select_worker(workers)
    assert result == 42


def test_select_worker_prefers_low_cpu(lb):
    """Воркер с cpu_load=0.1 предпочтительнее чем cpu_load=0.9."""
    workers = {
        1: _make_state(worker_id=1, cpu_load=0.9),
        2: _make_state(worker_id=2, cpu_load=0.1),
    }
    result = lb.select_worker(workers)
    assert result == 2


def test_select_worker_prefers_more_free_threads(lb):
    """Воркер с бóльшим количеством свободных потоков предпочтительнее."""
    workers = {
        1: _make_state(worker_id=1, cpu_load=0.5, free_threads=1, max_threads=4),
        2: _make_state(worker_id=2, cpu_load=0.5, free_threads=4, max_threads=4),
    }
    result = lb.select_worker(workers)
    assert result == 2


def test_all_workers_busy(lb):
    """Все загружены, но выбирает наименее загруженного."""
    workers = {
        1: _make_state(worker_id=1, cpu_load=0.95, free_threads=0, max_threads=4),
        2: _make_state(worker_id=2, cpu_load=0.80, free_threads=1, max_threads=4),
        3: _make_state(worker_id=3, cpu_load=0.60, free_threads=2, max_threads=4),
    }
    result = lb.select_worker(workers)
    assert result == 3


# === Тесты _score ===

def test_score_calculation(lb):
    """Проверить формулу score = 0.7×cpu + 0.3×(1 - free_ratio)."""
    # free_ratio = 2/4 = 0.5, (1 - 0.5) = 0.5
    state = _make_state(cpu_load=0.5, free_threads=2, max_threads=4)
    score = lb._score(state)
    expected = 0.7 * 0.5 + 0.3 * 0.5
    assert abs(score - expected) < 1e-9, f"score={score}, expected={expected}"


def test_score_zero_state(lb):
    """Score для воркера с нулевыми метриками и максимальными свободными = 0."""
    state = _make_state(cpu_load=0.0, free_threads=4, max_threads=4)
    score = lb._score(state)
    # free_ratio = 4/4 = 1.0, (1 - 1.0) = 0.0
    assert score == 0.0


def test_score_no_free_threads(lb):
    """Score максимальный при отсутствии свободных потоков."""
    state = _make_state(cpu_load=1.0, free_threads=0, max_threads=4)
    score = lb._score(state)
    # free_ratio = 0/4 = 0.0, (1 - 0.0) = 1.0
    expected = 0.7 * 1.0 + 0.3 * 1.0
    assert abs(score - expected) < 1e-9


def test_score_all_free_threads(lb):
    """Score минимальный при максимальном количестве свободных потоков."""
    state = _make_state(cpu_load=0.0, free_threads=10, max_threads=10)
    score = lb._score(state)
    # free_ratio = 10/10 = 1.0, (1 - 1.0) = 0.0
    assert score == 0.0


def test_score_prefers_more_free_threads(lb):
    """Воркер с бóльшим количеством свободных потоков предпочтительнее."""
    state_low_free = _make_state(cpu_load=0.5, free_threads=1, max_threads=4)
    state_high_free = _make_state(cpu_load=0.5, free_threads=3, max_threads=4)
    assert lb._score(state_high_free) < lb._score(state_low_free)


# === Тесты update_worker_state ===

def test_update_worker_state(lb):
    """Состояние обновляется и доступно через select_worker."""
    state = _make_state(worker_id=7, cpu_load=0.3)
    lb.update_worker_state(7, state)

    # select_worker на внутреннем dict
    result = lb.select_worker({7: state})
    assert result == 7
    assert lb._workers[7].cpu_load == 0.3


def test_update_worker_state_replaces(lb):
    """Повторное обновление заменяет предыдущее состояние."""
    state1 = _make_state(worker_id=1, cpu_load=0.5)
    state2 = _make_state(worker_id=1, cpu_load=0.9)
    lb.update_worker_state(1, state1)
    lb.update_worker_state(1, state2)
    assert lb._workers[1].cpu_load == 0.9


# === Краевые случаи ===

def test_score_all_equal(lb):
    """При одинаковых score выбирает первый попавшийся (определённость)."""
    workers = {
        1: _make_state(worker_id=1, cpu_load=0.5, free_threads=2, max_threads=4),
        2: _make_state(worker_id=2, cpu_load=0.5, free_threads=2, max_threads=4),
    }
    result = lb.select_worker(workers)
    assert result in (1, 2)


def test_stale_penalty_increases_score(lb):
    """stale_penalty не влияет на score (новая формула без stale)."""
    # Новая формула: 0.7×cpu + 0.3×(1 - free_ratio), stale не учитывается
    state_clean = _make_state(cpu_load=0.5, free_threads=2, max_threads=4)
    state_stale = _make_state(cpu_load=0.5, free_threads=2, max_threads=4, stale_penalty=1.0)
    # stale_penalty не влияет на score — они равны
    assert abs(lb._score(state_clean) - lb._score(state_stale)) < 1e-9


def test_max_threads_zero_prevents_division_by_zero(lb):
    """max_threads=0 не вызывает division by zero (safe division)."""
    state = _make_state(cpu_load=0.5, free_threads=0, max_threads=0)
    score = lb._score(state)
    # free_ratio = 0 / max(0, 1) = 0, (1 - 0) = 1.0
    expected = 0.7 * 0.5 + 0.3 * 1.0
    assert abs(score - expected) < 1e-9


def test_increment_decrement_active(lb):
    """increment_active / decrement_active корректно меняют active_tasks."""
    state = _make_state(worker_id=1, active_tasks=2)
    lb.update_worker_state(1, state)

    lb.increment_active(1)
    assert lb._workers[1].active_tasks == 3

    lb.decrement_active(1)
    assert lb._workers[1].active_tasks == 2

    # Не уходит ниже 0
    lb.decrement_active(1)
    lb.decrement_active(1)
    lb.decrement_active(1)
    assert lb._workers[1].active_tasks == 0


def test_select_worker_prefers_more_free_threads(lb):
    """При одинаковом cpu_load выбирает воркер с бóльшим free_threads."""
    workers = {
        1: _make_state(worker_id=1, cpu_load=0.5, free_threads=1, max_threads=4),
        2: _make_state(worker_id=2, cpu_load=0.5, free_threads=4, max_threads=4),
    }
    result = lb.select_worker(workers)
    assert result == 2
