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
) -> WorkerState:
    """Хелпер для создания WorkerState."""
    return WorkerState(
        worker_id=worker_id,
        pid=pid,
        cpu_load=cpu_load,
        active_tasks=active_tasks,
        stale_penalty=stale_penalty,
        core_id=core_id,
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


def test_select_worker_prefers_few_tasks(lb):
    """Воркер с active_tasks=0 предпочтительнее чем active_tasks=5."""
    workers = {
        1: _make_state(worker_id=1, active_tasks=5),
        2: _make_state(worker_id=2, active_tasks=0),
    }
    result = lb.select_worker(workers)
    assert result == 2


def test_all_workers_busy(lb):
    """Все загружены, но выбирает наименее загруженного."""
    workers = {
        1: _make_state(worker_id=1, cpu_load=0.95, active_tasks=9),
        2: _make_state(worker_id=2, cpu_load=0.80, active_tasks=7),
        3: _make_state(worker_id=3, cpu_load=0.60, active_tasks=5),
    }
    result = lb.select_worker(workers)
    assert result == 3


# === Тесты _score ===

def test_score_calculation(lb):
    """Проверить формулу score = 0.7×cpu + 0.2×tasks + 0.1×stale."""
    state = _make_state(cpu_load=0.5, active_tasks=3, stale_penalty=0.2)
    score = lb._score(state)

    # Нормализованные задачи: min(3/10, 1.0) = 0.3
    expected = 0.7 * 0.5 + 0.2 * 0.3 + 0.1 * 0.2
    assert abs(score - expected) < 1e-9, f"score={score}, expected={expected}"


def test_score_zero_state(lb):
    """Score для воркера с нулевыми метриками = 0."""
    state = _make_state(cpu_load=0.0, active_tasks=0, stale_penalty=0.0)
    score = lb._score(state)
    assert score == 0.0


def test_score_max_tasks_normalized(lb):
    """Активные задачи > MAX_ACTIVE_TASKS нормализуются до 1.0."""
    state = _make_state(active_tasks=100)
    score = lb._score(state)
    # 0.2 * 1.0 = 0.2
    expected = 0.2
    assert abs(score - expected) < 1e-9


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
        1: _make_state(worker_id=1, cpu_load=0.5, active_tasks=5),
        2: _make_state(worker_id=2, cpu_load=0.5, active_tasks=5),
    }
    result = lb.select_worker(workers)
    assert result in (1, 2)


def test_stale_penalty_increases_score(lb):
    """stale_penalty увеличивает score."""
    state_clean = _make_state(cpu_load=0.5, active_tasks=3, stale_penalty=0.0)
    state_stale = _make_state(cpu_load=0.5, active_tasks=3, stale_penalty=1.0)
    assert lb._score(state_stale) > lb._score(state_clean)
