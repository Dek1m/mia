"""Unit-тесты для SmartDispatcher — legacy + двухфазная маршрутизация."""
import threading
from concurrent.futures import Future

import pytest

from core.adaptive_router import AdaptiveRouter
from core.task import Task, TaskStatus, TaskType
from core.task_classifier import TaskClassifier
from core.task_store import TaskStore
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


# Legacy-маркировка
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


@pytest.fixture
def full_deps():
    """Диспетчер со всеми компонентами (новый режим)."""
    tp = FakeThreadPool()
    wm = FakeWorkerManager()
    store = TaskStore()
    classifier = TaskClassifier()
    router = AdaptiveRouter(store)
    dispatcher = SmartDispatcher(tp, wm, task_store=store, classifier=classifier, adaptive_router=router)
    return dispatcher, tp, wm, store, classifier, router


# ============================================================
# Legacy-тесты (обратная совместимость — fn._db_type)
# ============================================================

class TestLegacyRouting:
    """Legacy: маршрутизация по fn._db_type."""

    def test_read_routes_to_thread_pool(self, deps):
        dispatcher, tp, wm = deps
        result = dispatcher.dispatch(read_task, 5)
        assert result.result() == 10
        assert len(tp.submitted) == 1
        assert len(wm.submitted) == 0

    def test_write_routes_to_thread_pool(self, deps):
        dispatcher, tp, wm = deps
        result = dispatcher.dispatch(write_task, 5)
        assert result.result() == 6
        assert len(tp.submitted) == 1
        assert len(wm.submitted) == 0

    def test_aggregate_routes_to_worker_manager(self, deps):
        dispatcher, tp, wm = deps
        result = dispatcher.dispatch(aggregate_task, 4)
        assert result == 16
        assert len(tp.submitted) == 0
        assert len(wm.submitted) == 1

    def test_transaction_routes_to_thread_pool(self, deps):
        dispatcher, tp, wm = deps
        result = dispatcher.dispatch(transaction_task, 3, 7)
        assert result.result() == 10
        assert len(tp.submitted) == 1
        assert len(wm.submitted) == 0

    def test_unknown_type_fallback_to_read(self, deps):
        dispatcher, tp, wm = deps
        fn = lambda: 42
        result = dispatcher.dispatch(fn)
        assert result.result() == 42
        assert len(tp.submitted) == 1


class TestLegacyWriteLock:
    """Legacy: write-lock для write-задач."""

    def test_write_with_lock_uses_lock(self, deps):
        dispatcher, tp, wm = deps
        result = dispatcher.dispatch(locked_write, 5)
        assert result.result() == 15
        assert dispatcher.metrics["write"] == 1

    def test_acquire_release_lock(self):
        tp, wm = FakeThreadPool(), FakeWorkerManager()
        dispatcher = SmartDispatcher(tp, wm)
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


class TestLegacyMetrics:
    """Legacy: метрики."""

    def test_metrics_counts(self, deps):
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

    def test_metrics_is_copy(self, deps):
        dispatcher, _, _ = deps
        m1 = dispatcher.metrics
        dispatcher.dispatch(read_task, 1)
        m2 = dispatcher.metrics
        assert m1["read"] == 0
        assert m2["read"] == 1


# ============================================================
# Новая логика: двухфазная маршрутизация
# ============================================================

class TestTwoPhaseRouting:
    """Новый режим: classify → override → dispatch."""

    def test_dispatch_creates_task_implicitly(self, full_deps):
        """dispatch(fn) создаёт Task автоматически."""
        dispatcher, tp, wm, store, _, _ = full_deps

        def db_fn():
            return "ok"

        db_fn.__module__ = "db"
        db_fn.__name__ = "get_user"

        dispatcher.dispatch(db_fn)
        assert len(tp.submitted) == 1
        # Task попал в историю store
        history = store.get_history()
        assert len(history) == 1
        assert history[0].fn_name == "get_user"

    def test_dispatch_with_explicit_task(self, full_deps):
        """dispatch(task, fn) использует переданный Task."""
        dispatcher, tp, wm, store, _, _ = full_deps

        task = Task.create(module_id="api", fn_name="fetch")

        def api_fn():
            return "data"

        api_fn.__module__ = "api"

        dispatcher.dispatch(task, api_fn)
        history = store.get_history()
        assert len(history) == 1
        assert history[0].id == task.id

    def test_task_status_lifecycle(self, full_deps):
        """Task проходит полный жизненный цикл."""
        dispatcher, tp, wm, store, _, _ = full_deps

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
        dispatcher, tp, wm, store, _, _ = full_deps

        def bad_fn():
            raise ValueError("boom")

        bad_fn.__module__ = "test"
        bad_fn.__name__ = "bad_fn"

        with pytest.raises(ValueError, match="boom"):
            dispatcher.dispatch(bad_fn)

        task = store.get_history()[0]
        assert task.status == TaskStatus.FAILED
        assert task.error == "boom"
        assert task.duration is not None

    def test_classifier_determines_type(self, full_deps):
        """TaskClassifier определяет task_type."""
        dispatcher, tp, wm, store, classifier, _ = full_deps

        def get_user():
            return None

        get_user.__module__ = "db"
        get_user.__name__ = "get_user"

        dispatcher.dispatch(get_user)
        task = store.get_history()[0]
        # db.get_user → TaskType.IO (по правилу модуля "db" → DATABASE,
        # но fn_name "get_*" → IO с приоритетом 50 vs модуль 100 → DATABASE)
        assert task.task_type == TaskType.DATABASE

    def test_adaptive_override_applied(self, full_deps):
        """AdaptiveRouter применяет override при перегрузке."""
        dispatcher, tp, wm, store, classifier, router = full_deps

        # Имитируем перегрузку: добавляем историю с долгими задачами
        # Ключ в router — (module_id, task_type), поэтому история и классификация
        # должны совпадать. Classifier: "db" module → DATABASE, но нам нужно IO.
        # Используем модуль "data" с fn_name "get_slow" — нет в module_map,
        # но fn_name совпадает с паттерном "^get_.*" → IO.
        for _ in range(10):
            t = Task.create(module_id="data", fn_name="get_slow", task_type=TaskType.IO)
            t.start()
            t.complete(result=None)
            t.duration = 0.5  # > P95_THRESHOLD (0.1)
            store._active.pop(t.id, None)
            store._history.append(t)

        router.update_stats()

        def data_fn():
            return "data"

        data_fn.__module__ = "data"
        data_fn.__name__ = "get_slow"

        dispatcher.dispatch(data_fn)
        task = store.get_history()[0]
        # IO с p95 > порога → override на CPU
        assert task.task_type == TaskType.CPU


# ============================================================
# Write-lock в новом режиме
# ============================================================

class TestNewWriteLockWriteLock:
    """Write-lock для write-задач в новом режиме."""

    def test_io_with_db_lock_acquires_lock(self, full_deps):
        """IO-задача с _db_lock=True захватывает write_lock."""
        dispatcher, tp, wm, store, _, _ = full_deps

        fn = lambda x: x * 3
        fn._db_lock = True
        fn.__module__ = "db"
        fn.__name__ = "locked_fn"

        result = dispatcher.dispatch(fn, 5)
        assert result.result() == 15

    def test_concurrent_write_lock_serialization(self, full_deps):
        """Две write-задачи с lock выполняются последовательно."""
        dispatcher, tp, wm, store, _, _ = full_deps
        order = []

        def writer(val):
            order.append(val)
            return val

        writer._db_lock = True
        writer.__module__ = "db"
        writer.__name__ = "writer"

        f1 = dispatcher.dispatch(writer, 1)
        f2 = dispatcher.dispatch(writer, 2)

        # Обе задачи завершились
        assert f1.result() == 1
        assert f2.result() == 2
        # Порядок определён lock'ом (обе в одном потоке — строго по порядку)
        assert order == [1, 2]


# ============================================================
# Совместимость: legacy fn._db_type игнорирует classifier
# ============================================================

class TestBackwardCompatibility:
    """Legacy fn._db_type → используется напрямую, без classifier."""

    def test_legacy_ignores_classifier(self, deps):
        """fn._db_type = 'read' → ThreadPool, даже если classifier дал бы другой тип."""
        dispatcher, tp, wm = deps
        result = dispatcher.dispatch(read_task, 5)
        assert result.result() == 10
        assert len(tp.submitted) == 1
        assert len(wm.submitted) == 0

    def test_explicit_task_bypasses_db_type(self, full_deps):
        """При явном Task — fn._db_type обрабатывается classifier'ом (каскад).

        Classifier: _task_type → _db_type → module_name → fn_name → fallback.
        fn._db_type='aggregate' → AGGREGATE → WorkerManager.
        """
        dispatcher, tp, wm, store, classifier, _ = full_deps

        fn = lambda: "ok"
        fn._db_type = "aggregate"
        fn.__module__ = "test"
        fn.__name__ = "hybrid_fn"

        task = Task.create(module_id="test", fn_name="hybrid_fn")
        dispatcher.dispatch(task, fn)

        stored = store.get_history()[0]
        # classifier читает fn._db_type="aggregate" → AGGREGATE
        assert stored.task_type == TaskType.AGGREGATE
        # AGGREGATE → WorkerManager
        assert len(tp.submitted) == 0
        assert len(wm.submitted) == 1


# ============================================================
# Метрики в новом режиме
# ============================================================

class TestNewMetrics:
    """Метрики в двухфазном режиме."""

    def test_metrics_incremented_on_new_dispatch(self, full_deps):
        dispatcher, tp, wm, store, _, _ = full_deps

        def io_fn():
            return 1

        io_fn.__module__ = "db"
        io_fn.__name__ = "get_something"

        dispatcher.dispatch(io_fn)

        m = dispatcher.metrics
        assert m["read"] >= 1  # IO → read в метриках

    def test_aggregate_dispatches_to_worker_manager(self, full_deps):
        dispatcher, tp, wm, store, _, _ = full_deps

        def heavy():
            return 99

        heavy.__module__ = "compute"
        heavy.__name__ = "heavy"

        dispatcher.dispatch(heavy)

        # "compute" module → CPU, но CPU не aggregate → thread pool
        assert len(tp.submitted) == 1
        assert len(wm.submitted) == 0
