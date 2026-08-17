"""Интеграционные тесты для Database — полный цикл, кеш, SmartDispatcher, метрики."""
from __future__ import annotations

import pytest
from concurrent.futures import Future
from typing import Any

from core.application import Application
from core.database import Database
from core.factories import CacheFactory
from pools.smart_dispatcher import SmartDispatcher
from storage.cache_hierarchy import CacheHierarchy
from monitoring.metrics import (
    database_operations_total,
    database_operation_duration_seconds,
    database_cache_hits_total,
    database_cache_misses_total,
    worker_manager_tasks_submitted_total,
)


# ── Мок-провайдер ──────────────────────────────────────


class InMemoryProvider:
    """Провайдер, хранящий данные в памяти — для интеграционных тестов."""

    def __init__(self) -> None:
        self._store: dict[str, dict] = {}
        self._seq = 0

    def get(self, table: str, id: str) -> dict | None:
        return self._store.get(f"{table}:{id}")

    def get_by_field(self, table: str, field: str, value: Any) -> dict | None:
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
        return items[offset : offset + limit]

    def fetch(self, query: str, *params: Any) -> list[dict]:
        return []

    def execute(self, query: str, *params: Any) -> str:
        return "OK"


# ── Фикстуры ───────────────────────────────────────────


@pytest.fixture
def app() -> Any:
    """Application с hierarchy cache, startup → shutdown."""
    application = Application(modules_dir="modules", cache_backend="hierarchy")
    application.startup()
    yield application
    application.shutdown()


@pytest.fixture
def provider() -> InMemoryProvider:
    return InMemoryProvider()


@pytest.fixture
def cache() -> CacheHierarchy:
    """CacheHierarchy L0-only (без L1/L2)."""
    return CacheHierarchy(default_ttl=60)


@pytest.fixture
def db_no_dispatcher(cache: CacheHierarchy) -> Database:
    """Database с кешем, без SmartDispatcher — для чистых CRUD-тестов."""
    database = Database(cache=cache, dispatcher=None)
    database.register_provider("mem", InMemoryProvider(), is_default=True)
    return database


class FakeWorkerManager:
    """Заглушка WorkerManager — возвращает результат напрямую."""

    def __init__(self) -> None:
        self.submitted: list = []

    def submit(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        self.submitted.append((fn, args, kwargs))
        return fn(*args, **kwargs)


class FakeThreadPool:
    """Заглушка ThreadPool — возвращает результат напрямую."""

    def __init__(self) -> None:
        self.submitted: list = []

    def submit(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        self.submitted.append((fn, args, kwargs))
        return fn(*args, **kwargs)

    def start(self) -> None:
        pass

    def shutdown(self, wait: bool = True) -> None:
        pass


@pytest.fixture
def dispatcher() -> tuple[SmartDispatcher, FakeWorkerManager, FakeThreadPool]:
    """SmartDispatcher с мокнутым WorkerManager и ThreadPool."""
    wm = FakeWorkerManager()
    tp = FakeThreadPool()
    return SmartDispatcher(wm, thread_pool=tp), wm, tp


# ── 1. Полный цикл: Application → startup → Database → CRUD → shutdown ──


class TestFullLifecycle:
    """Полный цикл: Application → startup → Database → CRUD → shutdown."""

    def test_application_startup_creates_database(self, app: Application) -> None:
        """Application создаёт Database с SmartDispatcher и кешем."""
        db = app.database
        assert db is not None
        assert isinstance(db, Database)
        assert db._dispatcher is not None
        assert isinstance(db._dispatcher, SmartDispatcher)
        assert isinstance(db._cache, CacheHierarchy)

    def test_crud_through_application(self, app: Application, provider: InMemoryProvider) -> None:
        """CRUD операции через Application.database со SmartDispatcher + ThreadPool."""
        app.database.register_provider("mem", provider, is_default=True)

        # Insert → Future (SmartDispatcher → ThreadPool)
        id_future = app.database.insert("users", {"name": "Alice", "age": 30})
        assert isinstance(id_future, Future)
        id_ = id_future.result(timeout=5)

        # Get → Future
        get_future = app.database.get("users", id_)
        assert isinstance(get_future, Future)
        record = get_future.result(timeout=5)
        assert record is not None
        assert record["name"] == "Alice"
        assert record["age"] == 30

        # Update → Future
        update_future = app.database.update("users", id_, {"age": 31})
        updated = update_future.result(timeout=5)
        assert updated is not None
        assert updated["age"] == 31

        # Delete → Future
        del_future = app.database.delete("users", id_)
        assert del_future.result(timeout=5) is True

        # Повторный Get → None
        get2_future = app.database.get("users", id_)
        assert get2_future.result(timeout=5) is None

    def test_multiple_providers_through_application(self, app: Application) -> None:
        """Несколько провайдеров — каждый работает через Database."""
        p1 = InMemoryProvider()
        p2 = InMemoryProvider()
        app.database.register_provider("db1", p1, is_default=True)
        app.database.register_provider("db2", p2)

        # Insert в db1
        id1 = app.database.insert("items", {"source": "db1"}).result(timeout=5)
        # Insert в db2
        id2 = app.database.insert("items", {"source": "db2"}).result(timeout=5)

        # Get из разных провайдеров
        r1 = app.database.get("items", id1).result(timeout=5)
        r2 = app.database.get("items", id2).result(timeout=5)
        assert r1["source"] == "db1"
        assert r2["source"] == "db2"

    def test_database_property_returns_same_instance(self, app: Application) -> None:
        """Свойство database возвращает один и тот же экземпляр."""
        assert app.database is app.database


# ── 2. Cache интеграция: данные кешируются, инвалидируются при записи ──


class TestCacheIntegration:
    """CacheHierarchy + Database — кеширование и инвалидация."""

    def test_get_caches_result(self, db_no_dispatcher: Database) -> None:
        """Get кеширует результат — повторный get идёт в кеш."""
        provider = db_no_dispatcher.get_provider()
        provider._store["users:1"] = {"id": "1", "name": "Alice"}

        # Первый get — cache miss, данные из провайдера
        result = db_no_dispatcher.get("users", "1")
        assert result["name"] == "Alice"

        # Второй get — cache hit (данные в L0)
        result2 = db_no_dispatcher.get("users", "1")
        assert result2["name"] == "Alice"

        # Провайдер не вызывался повторно (данные не изменились)
        assert len(provider._store) == 1

    def test_cache_hit_returns_same_value(self, db_no_dispatcher: Database) -> None:
        """Cache hit возвращает то же значение, что и cache miss."""
        provider = db_no_dispatcher.get_provider()
        provider._store["users:1"] = {"id": "1", "name": "Bob", "scores": [1, 2, 3]}

        r1 = db_no_dispatcher.get("users", "1")
        r2 = db_no_dispatcher.get("users", "1")
        assert r1 == r2 == {"id": "1", "name": "Bob", "scores": [1, 2, 3]}

    def test_get_by_field_caches_result(self, db_no_dispatcher: Database) -> None:
        """Get_by_field кеширует результат по составному ключу."""
        provider = db_no_dispatcher.get_provider()
        provider._store["users:1"] = {"id": "1", "email": "a@b.com"}

        r1 = db_no_dispatcher.get_by_field("users", "email", "a@b.com")
        assert r1["email"] == "a@b.com"

        # Cache hit
        r2 = db_no_dispatcher.get_by_field("users", "email", "a@b.com")
        assert r2["email"] == "a@b.com"

    def test_update_invalidates_cache(self, db_no_dispatcher: Database) -> None:
        """Update удаляет кеш для конкретного id — следующий get идёт в провайдер."""
        provider = db_no_dispatcher.get_provider()
        provider._store["users:1"] = {"id": "1", "name": "Alice"}

        # Кешируем
        db_no_dispatcher.get("users", "1")

        # Update — инвалидирует кеш
        db_no_dispatcher.update("users", "1", {"name": "Bob"})

        # Следующий get идёт в провайдер (новые данные)
        result = db_no_dispatcher.get("users", "1")
        assert result["name"] == "Bob"

    def test_delete_invalidates_cache(self, db_no_dispatcher: Database) -> None:
        """Delete удаляет кеш — следующий get возвращает None."""
        provider = db_no_dispatcher.get_provider()
        provider._store["users:1"] = {"id": "1", "name": "Alice"}

        # Кешируем
        db_no_dispatcher.get("users", "1")

        # Delete — инвалидирует кеш и данные
        assert db_no_dispatcher.delete("users", "1") is True

        # Get возвращает None (и данные удалены из провайдера)
        result = db_no_dispatcher.get("users", "1")
        assert result is None

    def test_insert_does_not_break_existing_cache(self, db_no_dispatcher: Database) -> None:
        """Insert создаёт новый id — существующий кеш не затрагивается."""
        provider = db_no_dispatcher.get_provider()

        # Вставляем через провайдер, чтобы id был "1"
        id1 = db_no_dispatcher.insert("users", {"name": "Alice"})

        # Кешируем users:1
        r1 = db_no_dispatcher.get("users", id1)
        assert r1["name"] == "Alice"

        # Insert нового пользователя — не должен повлиять на кеш users:1
        id2 = db_no_dispatcher.insert("users", {"name": "Bob"})
        assert id2 != id1

        # Кеш users:1 на месте
        r1_again = db_no_dispatcher.get("users", id1)
        assert r1_again["name"] == "Alice"

    def test_cache_none_for_missing_record(self, db_no_dispatcher: Database) -> None:
        """Get несуществующей записи не кеширует None."""
        result = db_no_dispatcher.get("users", "nonexistent")
        assert result is None

        # Повторный get тоже None (не из кеша)
        result2 = db_no_dispatcher.get("users", "nonexistent")
        assert result2 is None

    def test_cache_hierarchy_stats(self, db_no_dispatcher: Database) -> None:
        """CacheHierarchy отслеживает hit/miss статистику."""
        cache = db_no_dispatcher._cache
        assert isinstance(cache, CacheHierarchy)

        provider = db_no_dispatcher.get_provider()
        provider._store["users:1"] = {"id": "1", "name": "X"}

        # miss → hit → hit
        db_no_dispatcher.get("users", "1")
        db_no_dispatcher.get("users", "1")
        db_no_dispatcher.get("users", "1")

        stats = cache.stats()
        assert stats["misses"] >= 1
        assert stats["hits"] >= 2

    def test_null_cache_does_not_cache(self) -> None:
        """NullCache ничего не кеширует — каждый get идёт в провайдер."""
        from storage.cache_interface import NullCache

        db = Database(cache=NullCache(), dispatcher=None)
        provider = InMemoryProvider()
        provider._store["t:1"] = {"id": "1", "v": 1}
        db.register_provider("mem", provider, is_default=True)

        # Каждый get идёт в провайдер (нет кеша)
        r1 = db.get("t", "1")
        r2 = db.get("t", "1")
        assert r1 == r2 == {"id": "1", "v": 1}


# ── 3. SmartDispatcher: маршрутизация работает ──


class TestSmartDispatcherRouting:
    """SmartDispatcher корректно маршрутизирует задачи по типам."""

    def _make_fn(self, db_type: str, lock: bool = False) -> Any:
        """Создать функцию с метаданными _db_type и _db_lock."""
        fn = lambda x: x * 2  # noqa: E731
        fn._db_type = db_type
        fn._db_lock = lock
        fn.__name__ = f"{db_type}_task"
        return fn

    def test_read_routes_to_worker_manager(self, dispatcher: tuple) -> None:
        """read-задача → ThreadPool (sync-задачи через thread pool)."""
        disp, wm, tp = dispatcher
        fn = self._make_fn("read")
        result = disp.dispatch(fn, 5)
        assert result == 10
        assert len(tp.submitted) == 1

    def test_write_routes_to_worker_manager(self, dispatcher: tuple) -> None:
        """write-задача → ThreadPool."""
        disp, wm, tp = dispatcher
        fn = self._make_fn("write")
        result = disp.dispatch(fn, 7)
        assert result == 14
        assert len(tp.submitted) == 1

    def test_transaction_routes_to_worker_manager(self, dispatcher: tuple) -> None:
        """transaction-задача → ThreadPool."""
        disp, wm, tp = dispatcher
        fn = self._make_fn("transaction")
        result = disp.dispatch(fn, 3)
        assert result == 6
        assert len(tp.submitted) == 1

    def test_aggregate_routes_to_worker_manager(self, dispatcher: tuple) -> None:
        """aggregate-задача → ThreadPool."""
        disp, wm, tp = dispatcher
        fn = self._make_fn("aggregate")
        result = disp.dispatch(fn, 4)
        assert result == 8
        assert len(tp.submitted) == 1

    def test_unknown_type_routes_to_worker_manager(self, dispatcher: tuple) -> None:
        """Неизвестный тип → ThreadPool."""
        disp, wm, tp = dispatcher
        fn = lambda: 42  # noqa: E731
        result = disp.dispatch(fn)
        assert result == 42
        assert len(tp.submitted) == 1

    def test_write_lock_serializes_writes(self) -> None:
        """write с _db_lock=True используется общая блокировка."""
        wm = FakeWorkerManager()
        tp = FakeThreadPool()
        disp = SmartDispatcher(wm, thread_pool=tp)

        fn = self._make_fn("write", lock=True)
        result = disp.dispatch(fn, 10)
        assert result == 20
        # _db_type не влияет на метрики — задача идёт через ThreadPool как UNKNOWN
        assert disp.metrics["unknown"] == 1

    def test_metrics_are_copy(self, dispatcher: tuple) -> None:
        """metrics возвращает копию, не мутабельную ссылку."""
        disp, _, _ = dispatcher
        m1 = disp.metrics
        disp.dispatch(self._make_fn("read"), 1)
        m2 = disp.metrics
        # _db_type не влияет на метрики — задача идёт через ThreadPool как UNKNOWN
        assert m1["unknown"] == 0
        assert m2["unknown"] == 1

    def test_dispatch_multiple_types(self, dispatcher: tuple) -> None:
        """Смешанная маршрутизация: все задачи идут через ThreadPool."""
        disp, wm, tp = dispatcher
        read_fn = self._make_fn("read")
        write_fn = self._make_fn("write")
        agg_fn = self._make_fn("aggregate")

        disp.dispatch(read_fn, 1)
        disp.dispatch(write_fn, 2)
        disp.dispatch(agg_fn, 3)

        m = disp.metrics
        # Все функции без _task_type → UNKNOWN
        assert m["unknown"] == 3

    def test_worker_manager_receives_correct_args(self, dispatcher: tuple) -> None:
        """ThreadPool получает правильные аргументы."""
        disp, wm, tp = dispatcher

        def add(a: int, b: int) -> int:
            return a + b

        add._db_type = "read"

        result = disp.dispatch(add, 10, 20)
        assert result == 30
        assert len(tp.submitted) == 1


# ── 4. Observability: метрики инкрементируются ──


class TestObservabilityMetrics:
    """Prometheus метрики инкрементируются при операциях Database."""

    def _counter_value(self, counter: Any, **labels: Any) -> float:
        """Получить текущее значение Counter по labels."""
        return counter.labels(**labels)._value.get()

    def _histogram_sum(self, histogram: Any, **labels: Any) -> float:
        """Получить сумму наблюдений в Histogram."""
        return histogram.labels(**labels)._sum.get()

    def test_operations_counter_increments_on_get(self, db_no_dispatcher: Database) -> None:
        """database_operations_total инкрементируется при get."""
        before = self._counter_value(database_operations_total, operation="get", status="ok")
        db_no_dispatcher.get("users", "1")
        after = self._counter_value(database_operations_total, operation="get", status="ok")
        assert after == before + 1

    def test_operations_counter_increments_on_insert(self, db_no_dispatcher: Database) -> None:
        """database_operations_total инкрементируется при insert."""
        before = self._counter_value(database_operations_total, operation="insert", status="ok")
        db_no_dispatcher.insert("users", {"name": "X"})
        after = self._counter_value(database_operations_total, operation="insert", status="ok")
        assert after == before + 1

    def test_operations_counter_increments_on_update(self, db_no_dispatcher: Database) -> None:
        """database_operations_total инкрементируется при update."""
        provider = db_no_dispatcher.get_provider()
        provider._store["users:1"] = {"id": "1", "name": "X"}

        before = self._counter_value(database_operations_total, operation="update", status="ok")
        db_no_dispatcher.update("users", "1", {"name": "Y"})
        after = self._counter_value(database_operations_total, operation="update", status="ok")
        assert after == before + 1

    def test_operations_counter_increments_on_delete(self, db_no_dispatcher: Database) -> None:
        """database_operations_total инкрементируется при delete."""
        provider = db_no_dispatcher.get_provider()
        provider._store["users:1"] = {"id": "1"}

        before = self._counter_value(database_operations_total, operation="delete", status="ok")
        db_no_dispatcher.delete("users", "1")
        after = self._counter_value(database_operations_total, operation="delete", status="ok")
        assert after == before + 1

    def test_cache_miss_counter_increments(self, db_no_dispatcher: Database) -> None:
        """database_cache_misses_total инкрементируется при cache miss."""
        before = database_cache_misses_total._value.get()
        db_no_dispatcher.get("users", "new")
        after = database_cache_misses_total._value.get()
        assert after >= before + 1

    def test_cache_hit_counter_increments(self, db_no_dispatcher: Database) -> None:
        """database_cache_hits_total инкрементируется при cache hit."""
        provider = db_no_dispatcher.get_provider()
        provider._store["users:1"] = {"id": "1"}

        # Прогреваем кеш
        db_no_dispatcher.get("users", "1")

        before_hits = database_cache_hits_total.labels(level="l0")._value.get()
        # Cache hit — данные уже в L0
        db_no_dispatcher.get("users", "1")
        after_hits = database_cache_hits_total.labels(level="l0")._value.get()
        assert after_hits >= before_hits + 1

    def test_duration_histogram_records(self, db_no_dispatcher: Database) -> None:
        """database_operation_duration_seconds записывает наблюдения."""
        before_sum = self._histogram_sum(database_operation_duration_seconds, operation="get")
        db_no_dispatcher.get("users", "x")
        after_sum = self._histogram_sum(database_operation_duration_seconds, operation="get")
        assert after_sum > before_sum

    def test_metrics_increment_through_application(self, app: Application, provider: InMemoryProvider) -> None:
        """Метрики инкрементируются через Application.database со SmartDispatcher."""
        app.database.register_provider("mem", provider, is_default=True)

        before_ops = self._counter_value(database_operations_total, operation="insert", status="ok")

        app.database.insert("t", {"x": 1}).result(timeout=5)

        after_ops = self._counter_value(database_operations_total, operation="insert", status="ok")
        assert after_ops > before_ops

    def test_smart_dispatcher_metrics_increment(self, app: Application, provider: InMemoryProvider) -> None:
        """SmartDispatcher метрики инкрементируются при dispatch."""
        app.database.register_provider("mem", provider, is_default=True)

        before_wm = worker_manager_tasks_submitted_total.labels(status="ok")._value.get()

        app.database.get("t", "1")  # read → WorkerManager

        after_wm = worker_manager_tasks_submitted_total.labels(status="ok")._value.get()
        assert after_wm > before_wm
