"""Unit-тесты для SmartDispatcher — простая маршрутизация задач."""
import threading
from concurrent.futures import Future

import pytest

from core.task import Task, TaskStatus, TaskType
from core.task_store import TaskStore
from pools.smart_dispatcher import SmartDispatcher


# === Вспомогательные функции ===

def simple_task(x):
    return x * 2


def aggregate_task(x):
    return x ** 2


def failing_task():
    raise ValueError("boom")


class FakeWorkerManager:
    """Заглушка WorkerManager для тестов."""

    def __init__(self):
        self.submitted = []

    def submit(self, fn, *args, **kwargs):
        self.submitted.append((fn, args, kwargs))
        return fn(*args, **kwargs)


class FakeThreadPool:
    """Заглушка ThreadPool для тестов (dispatch sync-задач)."""

    def __init__(self):
        self.submitted = []

    def submit(self, fn, *args, **kwargs):
        self.submitted.append((fn, args, kwargs))
        return fn(*args, **kwargs)

    def start(self):
        pass

    def shutdown(self, wait=True):
        pass


@pytest.fixture
def deps():
    wm = FakeWorkerManager()
    tp = FakeThreadPool()
    dispatcher = SmartDispatcher(wm, thread_pool=tp)
    return dispatcher, wm, tp


@pytest.fixture
def full_deps():
    """Диспетчер с TaskStore."""
    wm = FakeWorkerManager()
    tp = FakeThreadPool()
    store = TaskStore()
    dispatcher = SmartDispatcher(wm, thread_pool=tp, task_store=store)
    return dispatcher, wm, tp, store


# ============================================================
# Базовая маршрутизация
# ============================================================

class TestBasicRouting:
    """Базовая маршрутизация: все задачи через WorkerManager."""

    def test_simple_task_routes_to_worker_manager(self, deps):
        dispatcher, wm, tp = deps
        result = dispatcher.dispatch(simple_task, 5)
        assert result == 10
        assert len(tp.submitted) == 1

    def test_aggregate_task_routes_to_worker_manager(self, deps):
        dispatcher, wm, tp = deps
        result = dispatcher.dispatch(aggregate_task, 4)
        assert result == 16
        assert len(tp.submitted) == 1

    def test_dispatch_with_explicit_task(self, full_deps):
        """dispatch(task, fn) использует переданный Task."""
        dispatcher, wm, tp, store = full_deps

        task = Task.create(module_id="api", fn_name="fetch")

        def api_fn():
            return "data"

        api_fn.__module__ = "api"

        dispatcher.dispatch(task, api_fn)
        history = store.get_history()
        assert len(history) == 1
        assert history[0].id == task.id


# ============================================================
# Жизненный цикл Task
# ============================================================

class TestTaskLifecycle:
    """Task проходит полный жизненный цикл."""

    def test_task_status_lifecycle(self, full_deps):
        """Task проходит PENDING → RUNNING → COMPLETED."""
        dispatcher, wm, tp, store = full_deps

        def ok_fn():
            return 42

        ok_fn.__module__ = "test"
        ok_fn.__name__ = "ok_fn"

        dispatcher.dispatch(ok_fn)

        task = store.get_history()[0]
        assert task.status == TaskStatus.COMPLETED
        assert task.result == 42
        assert task.duration is not None
        assert task.started_at is not None
        assert task.completed_at is not None

    def test_task_failed_on_exception(self, full_deps):
        """При исключении — задача помечается FAILED."""
        dispatcher, wm, tp, store = full_deps

        bad_fn = failing_task
        bad_fn.__module__ = "test"
        bad_fn.__name__ = "bad_fn"

        with pytest.raises(ValueError, match="boom"):
            dispatcher.dispatch(bad_fn)

        task = store.get_history()[0]
        assert task.status == TaskStatus.FAILED
        assert task.error == "boom"
        assert task.duration is not None


# ============================================================
# Write-lock
# ============================================================

class TestWriteLock:
    """Write-lock для write-задач."""

    def test_acquire_release_lock(self):
        wm = FakeWorkerManager()
        dispatcher = SmartDispatcher(wm)
        dispatcher.acquire_lock()

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


# ============================================================
# Метрики
# ============================================================

class TestMetrics:
    """Метрики маршрутизации."""

    def test_metrics_is_copy(self, deps):
        dispatcher, _, _ = deps
        m1 = dispatcher.metrics
        dispatcher.dispatch(simple_task, 1)
        m2 = dispatcher.metrics
        # simple_task не имеет _task_type → UNKNOWN → "unknown"
        assert m1["unknown"] == 0
        assert m2["unknown"] == 1


# ============================================================
# dispatch_async
# ============================================================

class TestDispatchAsync:
    """Асинхронная маршрутизация."""

    def test_sync_function_via_dispatch_async(self, deps):
        """sync-функция через dispatch_async работает."""
        dispatcher, wm, tp = deps

        def sync_fn(x: int) -> int:
            return x * 3

        future = dispatcher.dispatch_async(sync_fn, 4)
        assert isinstance(future, Future)
        assert future.result() == 12
        assert len(wm.submitted) == 1

    def test_dispatch_async_with_explicit_task(self, deps):
        """dispatch_async с явным Task-объектом."""
        dispatcher, wm, tp = deps

        def sync_fn(x: int) -> int:
            return x + 10

        task_obj = Task.create(module_id="test", fn_name="sync_fn")
        future = dispatcher.dispatch_async(task_obj, sync_fn, 3)
        assert future.result() == 13
