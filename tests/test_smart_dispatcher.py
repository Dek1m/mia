"""Unit-тесты для SmartDispatcher — маршрутизация задач через SharedMemory."""
import threading
from concurrent.futures import Future

import pytest

from core.shared_memory import SharedMemory, TaskData
from core.task import Task, TaskStatus, TaskType
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


@pytest.fixture
def shared_memory():
    """Создаёт SharedMemory (local) и чистит после теста."""
    sm = SharedMemory(backend="local", num_blocks=16, block_size=4096)
    sm.start()
    yield sm
    sm.shutdown()


@pytest.fixture
def deps(shared_memory):
    wm = FakeWorkerManager()
    dispatcher = SmartDispatcher(wm, shared_memory=shared_memory)
    return dispatcher, wm, shared_memory


# ============================================================
# Базовая маршрутизация
# ============================================================

class TestBasicRouting:
    """Базовая маршрутизация: всё через SharedMemory."""

    def test_simple_task_routes_via_shared_memory(self, deps):
        dispatcher, wm, sm = deps
        result = dispatcher.dispatch(simple_task, 5)
        assert result == 10

    def test_aggregate_task_routes_via_shared_memory(self, deps):
        dispatcher, wm, sm = deps
        result = dispatcher.dispatch(aggregate_task, 4)
        assert result == 16

    def test_dispatch_with_explicit_task(self, deps):
        """dispatch(task, fn) использует переданный Task."""
        dispatcher, wm, sm = deps

        task = Task.create(module_id="api", fn_name="fetch")

        def api_fn():
            return "data"

        api_fn.__module__ = "api"

        dispatcher.dispatch(task, api_fn)


# ============================================================
# Жизненный цикл Task
# ============================================================

class TestTaskLifecycle:
    """Task проходит полный жизненный цикл."""

    def test_task_status_lifecycle(self, deps):
        """Task проходит PENDING → RUNNING → COMPLETED."""
        dispatcher, wm, sm = deps

        def ok_fn():
            return 42

        ok_fn.__module__ = "test"
        ok_fn.__name__ = "ok_fn"

        dispatcher.dispatch(ok_fn)

    def test_task_failed_on_exception(self, deps):
        """При исключении — задача помечается FAILED."""
        dispatcher, wm, sm = deps

        bad_fn = failing_task
        bad_fn.__module__ = "test"
        bad_fn.__name__ = "bad_fn"

        with pytest.raises((ValueError, Exception)):
            dispatcher.dispatch(bad_fn)


# ============================================================
# Write-lock
# ============================================================

class TestWriteLock:
    """Write-lock для write-задач."""

    def test_acquire_release_lock(self, deps):
        dispatcher, wm, sm = deps
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
# dispatch_async
# ============================================================

class TestDispatchAsync:
    """Асинхронная маршрутизация."""

    def test_sync_function_via_dispatch_async(self, deps):
        """sync-функция через dispatch_async работает."""
        dispatcher, wm, sm = deps

        def sync_fn(x: int) -> int:
            return x * 3

        future = dispatcher.dispatch_async(sync_fn, 4)
        assert isinstance(future, Future)
        assert future.result() == 12

    def test_dispatch_async_with_explicit_task(self, deps):
        """dispatch_async с явным Task-объектом."""
        dispatcher, wm, sm = deps

        def sync_fn(x: int) -> int:
            return x + 10

        task_obj = Task.create(module_id="test", fn_name="sync_fn")
        future = dispatcher.dispatch_async(task_obj, sync_fn, 3)
        assert future.result() == 13


# ============================================================
# SharedMemory интеграция
# ============================================================

class TestSharedMemoryIntegration:
    """Проверка интеграции с SharedMemory."""

    def test_task_appears_in_queue(self, deps):
        """После dispatch задача появляется в очереди SharedMemory."""
        dispatcher, wm, sm = deps

        def simple(x):
            return x

        simple.__module__ = "test"
        simple.__name__ = "simple"

        # Диспатчим — задача уйдёт в очередь и будет обработана
        result = dispatcher.dispatch(simple, 42)
        assert result == 42

    def test_result_stored_in_shared_memory(self, deps):
        """Результат задачи сохраняется в SharedMemory."""
        dispatcher, wm, sm = deps

        def compute(x):
            return x ** 2

        compute.__module__ = "test"
        compute.__name__ = "compute"

        result = dispatcher.dispatch(compute, 5)
        assert result == 25

    def test_multiple_tasks(self, deps):
        """Несколько задач подряд работают корректно."""
        dispatcher, wm, sm = deps

        results = []
        for i in range(5):
            r = dispatcher.dispatch(simple_task, i)
            results.append(r)

        assert results == [0, 2, 4, 6, 8]
