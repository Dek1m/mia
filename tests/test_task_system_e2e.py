"""E2E-тесты Universal Task System — полный цикл всех компонентов."""
from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import Future
from unittest.mock import MagicMock

import pytest

from core.adaptive_router import AdaptiveRouter
from core.database import Database
from core.task import Task, TaskStatus, TaskType
from core.task_classifier import TaskClassifier
from core.task_decorator import task
from core.task_store import TaskStore
from pools.smart_dispatcher import SmartDispatcher


# ============================================================
# Мок-провайдер для Database
# ============================================================


class InMemoryProvider:
    """Провайдер, хранящий данные в памяти (без реальной БД)."""

    def __init__(self) -> None:
        self._store: dict[str, dict] = {}
        self._seq = 0

    def get(self, table: str, id: str) -> dict | None:
        return self._store.get(f"{table}:{id}")

    def get_by_field(self, table: str, field: str, value) -> dict | None:
        for key, record in self._store.items():
            if key.startswith(f"{table}:") and record.get(field) == value:
                return record
        return None

    def insert(self, table: str, data: dict) -> str:
        self._seq += 1
        id_ = str(self._seq)
        self._store[f"{table}:{id_}"] = {"id": id_, **data}
        return id_

    def update(self, table: str, id: str, data: dict) -> dict | None:
        key = f"{table}:{id}"
        if key in self._store:
            self._store[key].update(data)
            return dict(self._store[key])
        return None

    def delete(self, table: str, id: str) -> bool:
        key = f"{table}:{id}"
        if key in self._store:
            del self._store[key]
            return True
        return False

    def exists(self, table: str, id: str) -> bool:
        return f"{table}:{id}" in self._store

    def count(self, table: str, filters: dict | None = None) -> int:
        return sum(1 for k in self._store if k.startswith(f"{table}:"))

    def list(self, table: str, filters: dict | None = None, limit: int = 100, offset: int = 0) -> list[dict]:
        items = [v for k, v in self._store.items() if k.startswith(f"{table}:")]
        return items[offset: offset + limit]

    def fetch(self, query: str, *params) -> list[dict]:
        return []

    def execute(self, query: str, *params) -> str:
        return "OK"


# ============================================================
# Заглушки для SmartDispatcher
# ============================================================


class FakeThreadPool:
    """Синхронный ThreadPool — выполняет fn в submit()."""

    def __init__(self) -> None:
        self.submitted: list = []

    def submit(self, fn, *args, **kwargs):
        self.submitted.append((fn, args, kwargs))
        result = fn(*args, **kwargs)
        fut = Future()
        fut.set_result(result)
        return fut


class FakeWorkerManager:
    """Синхронный WorkerManager."""

    def __init__(self) -> None:
        self.submitted: list = []

    def submit(self, fn, *args, **kwargs):
        self.submitted.append((fn, args, kwargs))
        return fn(*args, **kwargs)


# ============================================================
# 1. Полный цикл: создание -> классификация -> маршрутизация -> выполнение -> статистика
# ============================================================


class TestFullCycle:
    """E2E: полный жизненный цикл задачи через все компоненты."""

    def test_full_pipeline(self):
        """Task -> classify -> dispatch -> execute -> stats."""
        store = TaskStore()
        classifier = TaskClassifier()
        router = AdaptiveRouter(store)
        tp = FakeThreadPool()
        wm = FakeWorkerManager()
        dispatcher = SmartDispatcher(tp, wm, task_store=store, classifier=classifier, adaptive_router=router)

        def get_user(user_id: str) -> dict:
            return {"id": user_id, "name": "Alice"}

        get_user.__module__ = "db"
        get_user.__name__ = "get_user"

        dispatcher.dispatch(get_user, "42")

        history = store.get_history()
        assert len(history) == 1
        t = history[0]

        # Классификация: модуль "db" -> DATABASE
        assert t.task_type == TaskType.DATABASE
        assert t.status == TaskStatus.COMPLETED
        assert t.result == {"id": "42", "name": "Alice"}
        assert t.started_at is not None
        assert t.completed_at is not None
        assert t.duration is not None
        assert t.duration >= 0

    def test_full_pipeline_with_explicit_task(self):
        """Явный Task -> classify -> dispatch -> execute -> stats."""
        store = TaskStore()
        classifier = TaskClassifier()
        router = AdaptiveRouter(store)
        tp = FakeThreadPool()
        wm = FakeWorkerManager()
        dispatcher = SmartDispatcher(tp, wm, task_store=store, classifier=classifier, adaptive_router=router)

        t = Task.create(module_id="api", fn_name="fetch_data")

        def api_fn(url: str) -> str:
            return f"data from {url}"

        api_fn.__module__ = "api"
        api_fn.__name__ = "fetch_data"

        dispatcher.dispatch(t, api_fn, "https://example.com")

        found = store.get(t.id)
        assert found is not None
        assert found.status == TaskStatus.COMPLETED
        assert found.result == "data from https://example.com"
        assert found.task_type == TaskType.NETWORK

    def test_full_pipeline_failure(self):
        """Задача падает -> FAILED + error в истории."""
        store = TaskStore()
        classifier = TaskClassifier()
        tp = FakeThreadPool()
        wm = FakeWorkerManager()
        dispatcher = SmartDispatcher(tp, wm, task_store=store, classifier=classifier)

        def bad_fn():
            raise ValueError("connection timeout")

        bad_fn.__module__ = "db"
        bad_fn.__name__ = "bad_query"

        with pytest.raises(ValueError, match="connection timeout"):
            dispatcher.dispatch(bad_fn)

        t = store.get_history()[0]
        assert t.status == TaskStatus.FAILED
        assert t.error == "connection timeout"
        assert t.duration is not None

    def test_full_pipeline_multiple_tasks(self):
        """Несколько задач подряд — все корректно в истории."""
        store = TaskStore()
        classifier = TaskClassifier()
        tp = FakeThreadPool()
        wm = FakeWorkerManager()
        dispatcher = SmartDispatcher(tp, wm, task_store=store, classifier=classifier)

        for i in range(10):
            fn = lambda x, i=i: x + i
            fn.__module__ = "db"
            fn.__name__ = f"task_{i}"
            dispatcher.dispatch(fn, i)

        history = store.get_history()
        assert len(history) == 10
        assert all(t.status == TaskStatus.COMPLETED for t in history)


# ============================================================
# 2. Adaptive override: 100+ медленных IO задач -> CPU
# ============================================================


class TestAdaptiveOverride:
    """E2E: AdaptiveRouter переключает тип при перегрузке."""

    def test_slow_io_overrides_to_cpu(self):
        """100+ медленных IO-задач -> p95 > порога -> CPU."""
        store = TaskStore()
        classifier = TaskClassifier()
        tp = FakeThreadPool()
        wm = FakeWorkerManager()
        dispatcher = SmartDispatcher(tp, wm, task_store=store, classifier=classifier)
        router = AdaptiveRouter(store)
        dispatcher._adaptive_router = router

        for i in range(110):
            t = Task.create(module_id="storage", fn_name=f"read_block_{i}", task_type=TaskType.IO)
            t.start()
            t.complete(result=None)
            t.duration = 0.5
            store._active.pop(t.id, None)
            store._history.append(t)

        router.update_stats()

        def slow_read(block_id: int) -> bytes:
            return b"data"

        slow_read.__module__ = "storage"
        slow_read.__name__ = "read_block_new"

        dispatcher.dispatch(slow_read, 999)

        t = store.get_history()[0]
        assert t.task_type == TaskType.CPU

    def test_no_override_when_fast(self):
        """Быстрые IO-задачи -> override не применяется."""
        store = TaskStore()
        classifier = TaskClassifier()
        tp = FakeThreadPool()
        wm = FakeWorkerManager()
        dispatcher = SmartDispatcher(tp, wm, task_store=store, classifier=classifier)
        router = AdaptiveRouter(store)
        dispatcher._adaptive_router = router

        for i in range(50):
            t = Task.create(module_id="storage", fn_name=f"read_{i}", task_type=TaskType.IO)
            t.start()
            t.complete(result=None)
            t.duration = 0.01
            store._active.pop(t.id, None)
            store._history.append(t)

        router.update_stats()

        def fast_read(x):
            return x

        fast_read.__module__ = "storage"
        fast_read.__name__ = "read_new"

        dispatcher.dispatch(fast_read, 1)

        t = store.get_history()[0]
        assert t.task_type == TaskType.IO

    def test_override_module_isolation(self):
        """Override работает per-module: storage перегружен, api — нет."""
        store = TaskStore()
        classifier = TaskClassifier()
        tp = FakeThreadPool()
        wm = FakeWorkerManager()
        dispatcher = SmartDispatcher(tp, wm, task_store=store, classifier=classifier)
        router = AdaptiveRouter(store)
        dispatcher._adaptive_router = router

        for i in range(110):
            t = Task.create(module_id="storage", fn_name=f"read_{i}", task_type=TaskType.IO)
            t.start()
            t.complete(result=None)
            t.duration = 0.5
            store._active.pop(t.id, None)
            store._history.append(t)

        for i in range(50):
            t = Task.create(module_id="api", fn_name=f"fetch_{i}", task_type=TaskType.NETWORK)
            t.start()
            t.complete(result=None)
            t.duration = 0.01
            store._active.pop(t.id, None)
            store._history.append(t)

        router.update_stats()

        assert router.override(Task.create(module_id="storage", fn_name="r", task_type=TaskType.IO)) == TaskType.CPU
        assert router.override(Task.create(module_id="api", fn_name="f", task_type=TaskType.NETWORK)) is None


# ============================================================
# 3. Overflow ring buffer: 25001 задача -> старые удаляются
# ============================================================


class TestOverflowRingBuffer:
    """E2E: ring buffer отбрасывает старые задачи при переполнении."""

    def test_overflow_25001_tasks(self):
        """25001 задача -> в history только последние 25000."""
        store = TaskStore(max_size=25000)

        for i in range(25001):
            t = Task.create(module_id="db", fn_name=f"task_{i}")
            store.add(t)
            store.complete(t)

        stats = store.stats()
        assert stats["history_size"] == 25000
        assert stats["completed"] == 25000
        assert stats["active"] == 0

        history = store.get_history(limit=25000)
        assert history[0].fn_name == "task_25000"
        assert history[-1].fn_name == "task_1"

    def test_overflow_preserves_newest(self):
        """После overflow в истории только новые задачи."""
        store = TaskStore(max_size=5)

        for i in range(10):
            t = Task.create(module_id="db", fn_name=f"f{i}")
            store.add(t)
            store.complete(t)

        history = store.get_history(limit=100)
        fn_names = [t.fn_name for t in history]
        assert fn_names == ["f9", "f8", "f7", "f6", "f5"]

    def test_overflow_active_not_affected(self):
        """Активные задачи не удаляются при overflow."""
        store = TaskStore(max_size=3)

        active = Task.create(module_id="db", fn_name="active_task")
        store.add(active)

        for i in range(10):
            t = Task.create(module_id="db", fn_name=f"done_{i}")
            store.add(t)
            store.complete(t)

        assert store.get(active.id) is active
        assert active.fn_name == "active_task"


# ============================================================
# 4. Конкурентное добавление: 10 потоков
# ============================================================


class TestConcurrentAddition:
    """E2E: 10 потоков одновременно добавляют задачи."""

    def test_10_threads_concurrent_add(self):
        """10 потоков x 100 задач = 1000 активных задач."""
        store = TaskStore()
        errors: list[Exception] = []
        barrier = threading.Barrier(10)

        def add_tasks(thread_id: int):
            try:
                barrier.wait(timeout=5)
                for i in range(100):
                    t = Task.create(module_id=f"thread_{thread_id}", fn_name=f"task_{i}")
                    store.add(t)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=add_tasks, args=(tid,)) for tid in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Errors: {errors}"
        assert len(store.get_active()) == 1000

    def test_10_threads_concurrent_add_complete(self):
        """10 потоков конкурентно добавляют и завершают задачи."""
        store = TaskStore()
        errors: list[Exception] = []

        def add_and_complete(prefix: str, count: int):
            try:
                for i in range(count):
                    t = Task.create(module_id="db", fn_name=f"{prefix}_{i}")
                    store.add(t)
                    store.start(t)
                    store.complete(t, result=f"{prefix}_{i}")
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=add_and_complete, args=(f"t{tid}", 100))
            for tid in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Errors: {errors}"
        stats = store.stats()
        assert stats["active"] == 0
        assert stats["completed"] == 1000

    def test_10_threads_with_overflow(self):
        """10 потоков x 3000 задач с max_size=25000 -> overflow."""
        store = TaskStore(max_size=25000)
        errors: list[Exception] = []

        def add_tasks(prefix: str, count: int):
            try:
                for i in range(count):
                    t = Task.create(module_id="db", fn_name=f"{prefix}_{i}")
                    store.add(t)
                    store.complete(t)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=add_tasks, args=(f"t{tid}", 3000))
            for tid in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"Errors: {errors}"
        stats = store.stats()
        # Overflow: ring buffer хранит max 25000, старые выброшены
        assert stats["history_size"] <= 25000
        assert stats["completed"] == stats["history_size"]


# ============================================================
# 5. Legacy совместимость: SmartDispatcher с fn._db_type
# ============================================================


class TestLegacyCompatibility:
    """E2E: SmartDispatcher корректно обрабатывает fn._db_type."""

    def test_legacy_read(self):
        """fn._db_type='read' -> ThreadPool."""
        tp = FakeThreadPool()
        wm = FakeWorkerManager()
        dispatcher = SmartDispatcher(tp, wm)

        def read_user(user_id: str) -> dict:
            return {"id": user_id}

        read_user._db_type = "read"
        read_user.__module__ = "db"
        read_user.__name__ = "read_user"

        result = dispatcher.dispatch(read_user, "1")
        assert result.result() == {"id": "1"}
        assert len(tp.submitted) == 1
        assert len(wm.submitted) == 0

    def test_legacy_write(self):
        """fn._db_type='write' -> ThreadPool."""
        tp = FakeThreadPool()
        wm = FakeWorkerManager()
        dispatcher = SmartDispatcher(tp, wm)

        def write_user(data: dict) -> str:
            return "ok"

        write_user._db_type = "write"
        write_user.__module__ = "db"
        write_user.__name__ = "write_user"

        result = dispatcher.dispatch(write_user, {"name": "Bob"})
        assert result.result() == "ok"
        assert dispatcher.metrics["write"] == 1

    def test_legacy_aggregate(self):
        """fn._db_type='aggregate' -> WorkerManager."""
        tp = FakeThreadPool()
        wm = FakeWorkerManager()
        dispatcher = SmartDispatcher(tp, wm)

        def compute_stats() -> int:
            return 42

        compute_stats._db_type = "aggregate"
        compute_stats.__module__ = "compute"
        compute_stats.__name__ = "compute_stats"

        result = dispatcher.dispatch(compute_stats)
        assert result == 42
        assert len(tp.submitted) == 0
        assert len(wm.submitted) == 1
        assert dispatcher.metrics["aggregate"] == 1

    def test_legacy_transaction(self):
        """fn._db_type='transaction' -> ThreadPool."""
        tp = FakeThreadPool()
        wm = FakeWorkerManager()
        dispatcher = SmartDispatcher(tp, wm)

        def transfer(from_id: str, to_id: str, amount: int) -> bool:
            return True

        transfer._db_type = "transaction"
        transfer.__module__ = "db"
        transfer.__name__ = "transfer"

        result = dispatcher.dispatch(transfer, "A", "B", 100)
        assert result.result() is True
        assert len(tp.submitted) == 1
        assert dispatcher.metrics["transaction"] == 1

    def test_legacy_write_lock(self):
        """fn._db_type='write' + _db_lock=True -> write_lock используется."""
        tp = FakeThreadPool()
        wm = FakeWorkerManager()
        dispatcher = SmartDispatcher(tp, wm)

        order = []

        def locked_write(val: int) -> int:
            order.append(val)
            return val

        locked_write._db_type = "write"
        locked_write._db_lock = True
        locked_write.__module__ = "db"
        locked_write.__name__ = "locked_write"

        f1 = dispatcher.dispatch(locked_write, 1)
        f2 = dispatcher.dispatch(locked_write, 2)

        assert f1.result() == 1
        assert f2.result() == 2
        assert order == [1, 2]

    def test_legacy_mix_with_new(self):
        """Legacy и новые задачи вперемешку."""
        tp = FakeThreadPool()
        wm = FakeWorkerManager()
        store = TaskStore()
        classifier = TaskClassifier()
        router = AdaptiveRouter(store)
        dispatcher = SmartDispatcher(tp, wm, task_store=store, classifier=classifier, adaptive_router=router)

        # Legacy: fn._db_type = "read"
        def legacy_read(x):
            return x * 2

        legacy_read._db_type = "read"
        legacy_read.__module__ = "db"
        legacy_read.__name__ = "legacy_read"

        # Новый: без _db_type
        def new_fn(x):
            return x + 1

        new_fn.__module__ = "api"
        new_fn.__name__ = "new_fn"

        dispatcher.dispatch(legacy_read, 5)
        dispatcher.dispatch(new_fn, 10)

        assert len(tp.submitted) == 2
        history = store.get_history()
        # Новый режим создаёт задачи в store, legacy — нет
        assert len(history) == 1
        assert history[0].fn_name == "new_fn"


# ============================================================
# 6. @task декоратор: задача с @task автоматически создаёт Task
# ============================================================


class TestTaskDecoratorE2E:
    """E2E: @task декоратор интегрируется с Universal Task System."""

    def test_decorator_sets_task_type(self):
        """@task(type='cpu') устанавливает _task_type на функции."""
        @task(type="cpu", timeout=5.0, retry=3)
        def heavy_compute(data: list[int]) -> int:
            return sum(data) * 2

        assert heavy_compute._task_type == TaskType.CPU
        assert heavy_compute._task_timeout == 5.0
        assert heavy_compute._task_retry == 3

    def test_decorator_execution(self):
        """@task выполняет функцию и возвращает результат."""
        @task(type="io")
        def read_data(path: str) -> str:
            return f"contents of {path}"

        result = read_data("/etc/hosts")
        assert result == "contents of /etc/hosts"

    def test_decorator_retry(self):
        """@task с retry повторяет при ошибках."""
        call_count = 0

        @task(type="cpu", retry=2, retry_delay=0.01)
        def flaky() -> int:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ValueError("transient error")
            return 42

        result = flaky()
        assert result == 42
        assert call_count == 3

    def test_decorator_classifier_integration(self):
        """Classifier корректно классифицирует функцию с @task."""
        classifier = TaskClassifier()

        @task(type="database")
        def db_query(sql: str) -> list:
            return [{"result": sql}]

        t = Task.create(module_id="core", fn_name="db_query")
        task_type = classifier.classify(t, db_query)
        # _task_type = DATABASE имеет приоритет над правилами модуля/функции
        assert task_type == TaskType.DATABASE

    def test_decorator_async(self):
        """@task работает с async функциями."""
        @task(type="cpu")
        async def async_compute(x: int) -> int:
            return x * 3

        result = asyncio.run(async_compute(7))
        assert result == 21

    def test_decorator_preserves_metadata(self):
        """@task сохраняет __name__, __doc__, __module__."""
        @task(type="network")
        def fetch_url(url: str) -> str:
            """Fetch URL content."""
            return f"data from {url}"

        assert fetch_url.__name__ == "fetch_url"
        assert fetch_url.__doc__ == "Fetch URL content."

    def test_decorator_creates_task_object(self):
        """@task создаёт Task-объект при вызове."""
        created_tasks = []
        original_create = Task.create

        def capturing_create(*args, **kwargs):
            t = original_create(*args, **kwargs)
            created_tasks.append(t)
            return t

        Task.create = staticmethod(capturing_create)
        try:
            @task(type="io")
            def my_task(x: int) -> int:
                return x

            result = my_task(42)
            assert result == 42
            assert len(created_tasks) >= 1
            assert created_tasks[0].task_type == TaskType.IO
        finally:
            Task.create = staticmethod(original_create)


# ============================================================
# 7. Database integration: CRUD операции создают задачи
# ============================================================


class TestDatabaseIntegration:
    """E2E: Database facade создаёт задачи при CRUD-операциях."""

    @pytest.fixture
    def provider(self):
        return InMemoryProvider()

    @pytest.fixture
    def task_store(self):
        return TaskStore()

    @pytest.fixture
    def db(self, provider, task_store):
        stats_writer = MagicMock()
        db = Database(task_store=task_store, stats_writer=stats_writer)
        db.register_provider("mem", provider, is_default=True)
        return db, task_store, stats_writer, provider

    def test_insert_creates_task(self, db):
        """insert() создаёт задачу со статусом COMPLETED."""
        db_facade, task_store, _, _ = db

        result = db_facade.insert("users", {"name": "Alice"})
        assert result == "1"

        history = task_store.get_history()
        assert len(history) == 1
        t = history[0]
        assert t.fn_name == "insert"
        assert t.module_id == "database"
        assert t.task_type == TaskType.DATABASE
        assert t.status == TaskStatus.COMPLETED
        assert t.result == "1"

    def test_get_creates_task(self, db):
        """get() создаёт задачу с результатом."""
        db_facade, task_store, _, provider = db
        provider._store["users:1"] = {"id": "1", "name": "Bob"}

        result = db_facade.get("users", "1")
        assert result == {"id": "1", "name": "Bob"}

        history = task_store.get_history()
        assert len(history) == 1
        t = history[0]
        assert t.fn_name == "get"
        assert t.status == TaskStatus.COMPLETED
        assert t.result == {"id": "1", "name": "Bob"}

    def test_update_creates_task(self, db):
        """update() создаёт задачу."""
        db_facade, task_store, _, provider = db
        provider._store["users:1"] = {"id": "1", "name": "Alice"}

        result = db_facade.update("users", "1", {"name": "Bob"})
        assert result == {"id": "1", "name": "Bob"}

        history = task_store.get_history()
        assert len(history) == 1
        t = history[0]
        assert t.fn_name == "update"
        assert t.status == TaskStatus.COMPLETED

    def test_delete_creates_task(self, db):
        """delete() создаёт задачу."""
        db_facade, task_store, _, provider = db
        provider._store["users:1"] = {"id": "1"}

        result = db_facade.delete("users", "1")
        assert result is True

        history = task_store.get_history()
        assert len(history) == 1
        t = history[0]
        assert t.fn_name == "delete"
        assert t.status == TaskStatus.COMPLETED

    def test_crud_full_cycle(self, db):
        """Полный CRUD-цикл: insert -> get -> update -> delete."""
        db_facade, task_store, _, _ = db

        id1 = db_facade.insert("users", {"name": "Alice"})
        db_facade.get("users", id1)
        db_facade.update("users", id1, {"name": "Bob"})
        db_facade.delete("users", id1)

        history = task_store.get_history()
        assert len(history) == 4
        fn_names = [t.fn_name for t in history]
        assert fn_names == ["delete", "update", "get", "insert"]

    def test_error_creates_failed_task(self, db):
        """Ошибка в провайдере -> FAILED задача."""
        db_facade, task_store, _, _ = db

        class BrokenProvider:
            def get(self, table, id):
                raise RuntimeError("DB connection lost")
            def insert(self, table, data):
                return "1"

        db_facade.register_provider("broken", BrokenProvider(), is_default=True)

        with pytest.raises(RuntimeError, match="DB connection lost"):
            db_facade.get("t", "1")

        history = task_store.get_history()
        assert len(history) == 1
        t = history[0]
        assert t.status == TaskStatus.FAILED
        assert "DB connection lost" in t.error

    def test_stats_writer_receives_tasks(self, db):
        """StatsBatchWriter.add() вызывается для каждой операции."""
        db_facade, _, stats_writer, provider = db
        provider._store["users:1"] = {"id": "1"}

        db_facade.insert("users", {"name": "Alice"})
        db_facade.get("users", "1")

        assert stats_writer.add.call_count == 2
        # Проверяем что передаются Task-объекты
        for call in stats_writer.add.call_args_list:
            assert isinstance(call[0][0], Task)

    def test_task_has_timing(self, db):
        """Задачи имеют корректные тайминги."""
        db_facade, task_store, _, _ = db

        db_facade.insert("t", {"v": 1})
        t = task_store.get_history()[0]

        assert t.created_at > 0
        assert t.started_at is not None
        assert t.completed_at is not None
        assert t.duration is not None
        assert t.duration >= 0

    def test_without_task_system_backward_compat(self):
        """Database без Task System работает как раньше."""
        provider = InMemoryProvider()
        db = Database()
        db.register_provider("mem", provider, is_default=True)

        id_ = db.insert("t", {"v": 1})
        assert db.get("t", id_) == {"id": "1", "v": 1}
        assert db.update("t", id_, {"v": 2}) == {"id": "1", "v": 2}
        assert db.delete("t", id_) is True

    def test_factory_creates_all_components(self):
        """DatabaseFactory.create_with_task_system создаёт все компоненты."""
        from core.factories import DatabaseFactory

        database, task_store, stats_writer = DatabaseFactory.create_with_task_system()

        assert isinstance(database, Database)
        assert isinstance(task_store, TaskStore)
        assert database._task_store is task_store
        assert database._stats_writer is stats_writer
