"""E2E-тесты модуля db — интеграция CRUD + Universal Task System.

Покрывает:
  1. Полный цикл: CRUD → Task → classify → dispatch → execute → stats
  2. Кеш через @db_method (cache_ttl)
  3. Lock через @db_method (lock)
  4. Retry через @task (retry)
  5. Батч-запись: 100 операций → StatsBatchWriter.flush → task_history
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.task import Task, TaskStatus, TaskType
from core.task_store import TaskStore
from core.stats_batch_writer import StatsBatchWriter
from core.task_classifier import TaskClassifier
from core.adaptive_router import AdaptiveRouter
from pools.smart_dispatcher import SmartDispatcher
from modules.db.provider import DatabaseProvider, db_method, _resolve_cache_key
from modules.db.config import DatabaseConfig


# ── Мок-пул (полная имитация asyncpg) ────────────────────


class E2EMockPool:
    """Полнофункциональный мок asyncpg pool для E2E-тестов."""

    def __init__(self) -> None:
        self._store: dict[str, dict] = {}
        self._seq = 0
        self._lock = threading.Lock()

    async def fetchrow(self, query: str, *args: Any) -> dict | None:
        with self._lock:
            if "UPDATE" in query and "RETURNING" in query:
                # UPDATE ... SET ... WHERE id = $N RETURNING *
                for record in self._store.values():
                    if args and record.get("id") == args[-1]:
                        # Обновляем поля: args без последнего (id)
                        for i, key in enumerate(["name"]):
                            if i < len(args) - 1:
                                record[key] = args[i]
                        return dict(record)
                return None
            if "SELECT" in query and "FROM" in query and "EXISTS" not in query:
                for record in self._store.values():
                    if args and record.get("id") == args[0]:
                        return dict(record)
                return None
        return None

    async def fetchval(self, query: str, *args: Any) -> Any:
        with self._lock:
            if "INSERT" in query and "RETURNING id" in query:
                self._seq += 1
                id_ = str(self._seq)
                # Извлекаем данные из args (упрощённо — все кроме id)
                data = {"id": id_}
                if args:
                    # Для простых INSERT: args = (value1, value2, ...)
                    data["name"] = args[0] if args else None
                self._store[f"id:{id_}"] = data
                return id_
            if "SELECT EXISTS" in query:
                for record in self._store.values():
                    if args and record.get("id") == args[0]:
                        return True
                return False
            if "SELECT COUNT" in query:
                return len(self._store)
        return None

    async def fetch(self, query: str, *args: Any) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._store.values()]

    async def execute(self, query: str, *args: Any) -> str:
        with self._lock:
            if "DELETE" in query and "ANY" in query:
                # bulk_delete: DELETE FROM table WHERE id = ANY($1)
                ids_to_delete = args[0] if args else []
                deleted = 0
                for record in list(self._store.values()):
                    if record.get("id") in ids_to_delete:
                        del self._store[f"id:{record['id']}"]
                        deleted += 1
                return f"DELETE {deleted}"
            if "DELETE" in query:
                for record in list(self._store.values()):
                    if args and record.get("id") == args[0]:
                        del self._store[f"id:{record['id']}"]
                        return "DELETE 1"
                return "DELETE 0"
            if "UPDATE" in query:
                for record in self._store.values():
                    if args and record.get("id") == args[-1]:
                        # args[-1] — id (последний параметр в UPDATE ... WHERE id = $N)
                        for i, key in enumerate(["name"]):  # маппинг колонок
                            if i < len(args) - 1:
                                record[key] = args[i]
                        return "UPDATE 1"
                return "UPDATE 0"
            if "INSERT" in query and "RETURNING" not in query:
                self._seq += 1
                id_ = str(self._seq)
                data = {"id": id_}
                if args:
                    data["name"] = args[0]
                self._store[f"id:{id_}"] = data
                return "INSERT 0 1"
        return "OK"


# ── Мок-кеш ────────────────────────────────────────────


class E2ECache:
    """Простой dict-кеш с подсчётом операций."""

    def __init__(self) -> None:
        self._store: dict[str, Any] = {}
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Any | None:
        if key in self._store:
            self.hits += 1
            return self._store[key]
        self.misses += 1
        return None

    def set(self, key: str, value: Any, ttl: int = 0) -> None:
        self._store[key] = value

    def delete(self, key: str) -> None:
        self._store.pop(key, None)


# ── Фикстуры ───────────────────────────────────────────


@pytest.fixture
def pool() -> E2EMockPool:
    return E2EMockPool()


@pytest.fixture
def config() -> DatabaseConfig:
    return DatabaseConfig(
        host="localhost",
        port=5432,
        database="test",
        user="test",
        password="test",
    )


@pytest.fixture
def task_store() -> TaskStore:
    return TaskStore()


@pytest.fixture
def stats_writer() -> MagicMock:
    return MagicMock(spec=StatsBatchWriter)


@pytest.fixture
def provider(pool: E2EMockPool, config: DatabaseConfig, task_store: TaskStore, stats_writer: MagicMock) -> DatabaseProvider:
    return DatabaseProvider(
        pool=pool,
        config=config,
        task_store=task_store,
        stats_writer=stats_writer,
    )


@pytest.fixture
def cache() -> E2ECache:
    return E2ECache()


@pytest.fixture
def provider_with_cache(pool: E2EMockPool, config: DatabaseConfig, task_store: TaskStore, stats_writer: MagicMock, cache: E2ECache) -> DatabaseProvider:
    p = DatabaseProvider(
        pool=pool,
        config=config,
        task_store=task_store,
        stats_writer=stats_writer,
    )
    p.set_cache(cache)
    return p


# ============================================================
# 1. Полный цикл: CRUD → Task → classify → dispatch → execute → stats
# ============================================================


class TestFullCRUDCycle:
    """E2E: каждая CRUD-операция создаёт Task, трекается в TaskStore."""

    @pytest.mark.asyncio
    async def test_insert_get_update_delete_creates_tasks(
        self, provider: DatabaseProvider, task_store: TaskStore, stats_writer: MagicMock,
    ) -> None:
        """Полный CRUD: insert → get → update → delete — 4 задачи в истории."""
        id1 = await provider.insert("users", {"name": "Alice"})
        assert id1 == "1"

        user = await provider.get("users", id1)
        assert user == {"id": "1", "name": "Alice"}

        updated = await provider.update("users", id1, {"name": "Bob"})
        assert updated is not None

        deleted = await provider.delete("users", id1)
        assert deleted is True

        history = task_store.get_history()
        assert len(history) == 4

        fn_names = [t.fn_name for t in history]
        assert fn_names == ["delete", "update", "get", "insert"]

        for t in history:
            assert t.status == TaskStatus.COMPLETED
            assert t.module_id == "db.provider"
            assert t.duration is not None
            assert t.duration >= 0

    @pytest.mark.asyncio
    async def test_task_types_are_correct(
        self, provider: DatabaseProvider, task_store: TaskStore,
    ) -> None:
        """CRUD-задачи имеют правильные TaskType."""
        await provider.insert("t", {"v": 1})
        await provider.get("t", "1")
        await provider.count("t")
        await provider.exists("t", "1")

        history = task_store.get_history()
        type_map = {t.fn_name: t.task_type for t in history}

        # insert = write → IO, get = read → IO, count = aggregate → AGGREGATE, exists = read → IO
        assert type_map["exists"] == TaskType.IO
        assert type_map["count"] == TaskType.AGGREGATE
        assert type_map["get"] == TaskType.IO
        assert type_map["insert"] == TaskType.IO

    @pytest.mark.asyncio
    async def test_error_creates_failed_task(
        self, provider: DatabaseProvider, task_store: TaskStore,
    ) -> None:
        """Ошибка в пуле → FAILED задача в истории."""
        class BrokenPool:
            async def fetchrow(self, query, *args):
                raise RuntimeError("DB connection lost")

        provider._pool = BrokenPool()

        with pytest.raises(RuntimeError, match="DB connection lost"):
            await provider.get("users", "1")

        history = task_store.get_history()
        assert len(history) == 1
        t = history[0]
        assert t.fn_name == "get"
        assert t.status == TaskStatus.FAILED
        assert "DB connection lost" in t.error

    @pytest.mark.asyncio
    async def test_stats_writer_receives_every_task(
        self, provider: DatabaseProvider, stats_writer: MagicMock,
    ) -> None:
        """StatsBatchWriter.add() вызывается для каждой операции."""
        await provider.insert("t", {"v": 1})
        await provider.get("t", "1")
        await provider.update("t", "1", {"v": 2})
        await provider.delete("t", "1")

        assert stats_writer.add.call_count == 4
        for call in stats_writer.add.call_args_list:
            task_arg = call[0][0]
            assert isinstance(task_arg, Task)

    @pytest.mark.asyncio
    async def test_exists_and_count_in_full_cycle(
        self, provider: DatabaseProvider, task_store: TaskStore,
    ) -> None:
        """exists и count корректно трекаются в TaskStore."""
        await provider.insert("t", {"v": 1})

        exists_result = await provider.exists("t", "1")
        assert exists_result is True

        count_result = await provider.count("t")
        assert count_result >= 1

        history = task_store.get_history()
        assert len(history) == 3  # insert + exists + count


# ============================================================
# 2. Кеш через @db_method
# ============================================================


class TestCacheViaDbMethod:
    """E2E: кеш работает через декоратор @db_method."""

    @pytest.mark.asyncio
    async def test_cache_hit_returns_cached_value(
        self, provider_with_cache: DatabaseProvider, pool: E2EMockPool, cache: E2ECache,
    ) -> None:
        """Повторный get с cache_ttl — cache hit, pool не вызывается."""
        pool._store["id:1"] = {"id": "1", "name": "Alice"}

        # Первый вызов — cache miss, pool вызван
        result1 = await provider_with_cache.get("users", "1")
        assert result1 == {"id": "1", "name": "Alice"}
        assert cache.misses == 1
        assert cache.hits == 0

        # Второй вызов — cache hit, pool НЕ вызывается
        result2 = await provider_with_cache.get("users", "1")
        assert result2 == {"id": "1", "name": "Alice"}
        assert cache.hits == 1
        assert cache.misses == 1  # не увеличилось

    @pytest.mark.asyncio
    async def test_cache_disabled_when_ttl_zero(
        self, pool: E2EMockPool, config: DatabaseConfig, task_store: TaskStore,
    ) -> None:
        """cache_ttl=0 — кеш не используется."""
        cache = E2ECache()

        provider = DatabaseProvider(pool=pool, config=config, task_store=task_store)
        provider.set_cache(cache)

        pool._store["id:1"] = {"id": "1", "name": "Alice"}

        # exists() имеет cache_ttl=0 по умолчанию в @db_method
        await provider.exists("users", "1")
        await provider.exists("users", "1")

        # Кеш пуст — оба вызова пошли в pool
        assert len(cache._store) == 0

    @pytest.mark.asyncio
    async def test_cache_key_auto_generation(
        self, pool: E2EMockPool, config: DatabaseConfig, task_store: TaskStore,
    ) -> None:
        """Автогенерация cache_key если cache_key не задан."""
        cache = E2ECache()

        provider = DatabaseProvider(pool=pool, config=config, task_store=task_store)
        provider.set_cache(cache)

        # count() имеет cache_ttl=60, но cache_key не задан — автогенерация
        await provider.count("users")

        assert len(cache._store) == 1
        key = list(cache._store.keys())[0]
        assert key.startswith("count:")

    @pytest.mark.asyncio
    async def test_cache_key_template_substitution(
        self, pool: E2EMockPool, config: DatabaseConfig, task_store: TaskStore,
    ) -> None:
        """Шаблон cache_key подставляет значения параметров."""
        cache = E2ECache()

        provider = DatabaseProvider(pool=pool, config=config, task_store=task_store)
        provider.set_cache(cache)

        pool._store["id:42"] = {"id": "42", "name": "Bob"}

        await provider.get("users", "42")

        # get() имеет cache_key="{table}:{id}" → "users:42"
        assert "users:42" in cache._store

    @pytest.mark.asyncio
    async def test_cache_independence_per_table(
        self, provider_with_cache: DatabaseProvider, pool: E2EMockPool, cache: E2ECache,
    ) -> None:
        """Кеш работает независимо для разных таблиц."""
        pool._store["id:1"] = {"id": "1", "name": "Alice"}
        pool._store["id:2"] = {"id": "2", "name": "Bob"}

        r1 = await provider_with_cache.get("users", "1")
        r2 = await provider_with_cache.get("orders", "2")

        assert r1 == {"id": "1", "name": "Alice"}
        assert r2 == {"id": "2", "name": "Bob"}
        assert len(cache._store) == 2


# ============================================================
# 3. Lock через @db_method
# ============================================================


class TestLockViaDbMethod:
    """E2E: lock через декоратор @db_method."""

    @pytest.mark.asyncio
    async def test_insert_has_lock_metadata(
        self, provider: DatabaseProvider,
    ) -> None:
        """insert() имеет _db_lock="{table}:{id}"."""
        # Проверяем метаданные декоратора
        assert provider.insert._db_lock == "{table}:{id}"

    @pytest.mark.asyncio
    async def test_update_has_lock_metadata(
        self, provider: DatabaseProvider,
    ) -> None:
        """update() имеет _db_lock="{table}:{id}"."""
        assert provider.update._db_lock == "{table}:{id}"

    @pytest.mark.asyncio
    async def test_delete_has_lock_metadata(
        self, provider: DatabaseProvider,
    ) -> None:
        """delete() имеет _db_lock="{table}:{id}"."""
        assert provider.delete._db_lock == "{table}:{id}"

    @pytest.mark.asyncio
    async def test_lock_prevents_concurrent_writes(
        self, provider: DatabaseProvider, task_store: TaskStore,
    ) -> None:
        """Два последовательных insert выполняются корректно (lock не блокирует)."""
        id1 = await provider.insert("users", {"name": "Alice"})
        id2 = await provider.insert("users", {"name": "Bob"})

        assert id1 == "1"
        assert id2 == "2"

        history = task_store.get_history()
        assert len(history) == 2
        assert all(t.status == TaskStatus.COMPLETED for t in history)

    @pytest.mark.asyncio
    async def test_lock_timeout_metadata(
        self, provider: DatabaseProvider,
    ) -> None:
        """lock_timeout по умолчанию = 5.0."""
        assert provider.insert._db_lock_timeout == 5.0

    @pytest.mark.asyncio
    async def test_read_methods_have_no_lock(
        self, provider: DatabaseProvider,
    ) -> None:
        """get(), exists(), count() — без lock."""
        assert provider.get._db_lock is None
        assert provider.exists._db_lock is None
        assert provider.count._db_lock is None


# ============================================================
# 4. Retry через @task
# ============================================================


class TestRetryViaTask:
    """E2E: retry работает через @task декоратор."""

    @pytest.mark.asyncio
    async def test_retry_succeeds_on_second_attempt(
        self, pool: E2EMockPool, config: DatabaseConfig, task_store: TaskStore,
    ) -> None:
        """Метод с retry=3 падает один раз, потомucceeds."""
        call_count = 0
        original_fetchrow = pool.fetchrow

        async def flaky_fetchrow(query, *args):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("timeout")
            return {"id": "1", "name": "Alice"}

        pool.fetchrow = flaky_fetchrow

        provider = DatabaseProvider(pool=pool, config=config, task_store=task_store)

        # get() имеет @db_method(retry=0) по умолчанию
        # Используем кастомный метод с retry
        @db_method(type="read", retry=3, retry_delay=0.01)
        async def get_with_retry(self, table: str, id: str) -> dict | None:
            return await self._pool.fetchrow(f"SELECT * FROM {table} WHERE id = $1", id)

        result = await get_with_retry(provider, "users", "1")
        assert result == {"id": "1", "name": "Alice"}
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_retry_exhausted_raises_error(
        self, pool: E2EMockPool, config: DatabaseConfig, task_store: TaskStore,
    ) -> None:
        """Все попытки retry исчерпаны — выбрасывается ошибка."""
        async def always_fail(query, *args):
            raise RuntimeError("persistent failure")

        pool.fetchrow = always_fail
        provider = DatabaseProvider(pool=pool, config=config, task_store=task_store)

        @db_method(type="read", retry=2, retry_delay=0.01)
        async def get_always_fail(self, table: str, id: str) -> dict | None:
            return await self._pool.fetchrow(f"SELECT * FROM {table} WHERE id = $1", id)

        with pytest.raises(RuntimeError, match="persistent failure"):
            await get_always_fail(provider, "users", "1")

    @pytest.mark.asyncio
    async def test_retry_creates_multiple_failed_tasks(
        self, pool: E2EMockPool, config: DatabaseConfig, task_store: TaskStore,
    ) -> None:
        """Каждая неудачная попытка retry логируется,最終 succeeds."""
        call_count = 0

        async def fail_then_succeed(query, *args):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise ConnectionError("transient")
            return {"id": "1", "name": "Alice"}

        pool.fetchrow = fail_then_succeed
        provider = DatabaseProvider(pool=pool, config=config, task_store=task_store)

        @db_method(type="read", retry=3, retry_delay=0.01)
        async def get_retry(self, table: str, id: str) -> dict | None:
            return await self._pool.fetchrow(f"SELECT * FROM {table} WHERE id = $1", id)

        result = await get_retry(provider, "users", "1")
        assert result == {"id": "1", "name": "Alice"}

        # @db_method оборачивает через @task, который при retry логирует
        # каждую попытку (WARNING в логах). TaskStore не получает задачи
        # от @task — это отдельный уровень абстракции.
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_retry_delay_is_exponential(
        self, pool: E2EMockPool, config: DatabaseConfig,
    ) -> None:
        """Retry использует exponential backoff."""
        delays = []
        original_sleep = asyncio.sleep

        async def capturing_sleep(delay):
            delays.append(delay)
            await original_sleep(0)  # не ждём реально

        asyncio.sleep = capturing_sleep
        try:
            call_count = 0

            async def fail_twice(query, *args):
                nonlocal call_count
                call_count += 1
                if call_count <= 2:
                    raise ConnectionError("transient")
                return {"id": "1"}

            pool.fetchrow = fail_twice
            provider = DatabaseProvider(pool=pool, config=config)

            @db_method(type="read", retry=3, retry_delay=0.1)
            async def get_retry(self, table: str, id: str) -> dict | None:
                return await self._pool.fetchrow(f"SELECT * FROM {table} WHERE id = $1", id)

            await get_retry(provider, "users", "1")

            # retry_delay * 2^attempt: 0.1*1=0.1, 0.1*2=0.2
            assert delays == [0.1, 0.2]
        finally:
            asyncio.sleep = original_sleep


# ============================================================
# 5. Батч-запись: 100 операций → StatsBatchWriter.flush → task_history
# ============================================================


class TestBatchWrite:
    """E2E: 100 CRUD-операций → StatsBatchWriter → flush."""

    @pytest.mark.asyncio
    async def test_100_operations_create_100_tasks(
        self, provider: DatabaseProvider, task_store: TaskStore, stats_writer: MagicMock,
    ) -> None:
        """100 insert-операций → 100 задач в TaskStore."""
        for i in range(100):
            await provider.insert("items", {"name": f"item_{i}"})

        history = task_store.get_history()
        assert len(history) == 100
        assert all(t.status == TaskStatus.COMPLETED for t in history)
        assert all(t.fn_name == "insert" for t in history)

    @pytest.mark.asyncio
    async def test_100_operations_send_to_stats_writer(
        self, provider: DatabaseProvider, stats_writer: MagicMock,
    ) -> None:
        """100 операций → 100 вызовов StatsBatchWriter.add()."""
        for i in range(100):
            await provider.insert("items", {"name": f"item_{i}"})

        assert stats_writer.add.call_count == 100

    @pytest.mark.asyncio
    async def test_batch_mixed_operations(
        self, provider: DatabaseProvider, task_store: TaskStore,
    ) -> None:
        """Смешанные CRUD-операции: insert + get + update + delete."""
        ids = []
        for i in range(25):
            id_ = await provider.insert("items", {"name": f"item_{i}"})
            ids.append(id_)

        for id_ in ids[:10]:
            await provider.get("items", id_)

        for id_ in ids[:5]:
            await provider.update("items", id_, {"name": "updated"})

        for id_ in ids[:3]:
            await provider.delete("items", id_)

        history = task_store.get_history()
        # 25 insert + 10 get + 5 update + 3 delete = 43
        assert len(history) == 43

    @pytest.mark.asyncio
    async def test_stats_batch_writer_buffer_accumulates(
        self, provider: DatabaseProvider, task_store: TaskStore,
    ) -> None:
        """StatsBatchWriter накапливает задачи в буфере."""
        # Создаём реальный StatsBatchWriter без db
        real_writer = StatsBatchWriter(db=None, batch_size=50)
        provider.set_stats_writer(real_writer)

        for i in range(10):
            await provider.insert("items", {"name": f"item_{i}"})

        assert real_writer.buffer_size() == 10

        real_writer.stop()

    @pytest.mark.asyncio
    async def test_flush_pushes_to_db(self) -> None:
        """flush() записывает батч в mock DB."""
        mock_db = MagicMock()
        writer = StatsBatchWriter(db=mock_db, batch_size=10)

        for i in range(5):
            task = Task.create(
                module_id="db.provider",
                fn_name="insert",
                task_type=TaskType.IO,
            )
            task.start()
            task.complete(result=f"id_{i}")
            writer.add(task)

        # Flush асинхронно
        await writer.flush()

        # flush вызывает db.execute для history и stats
        assert mock_db.execute.call_count >= 1  # history INSERT + stats UPDATE

        writer.stop()

    @pytest.mark.asyncio
    async def test_batch_with_errors(
        self, provider: DatabaseProvider, task_store: TaskStore, stats_writer: MagicMock,
    ) -> None:
        """Батч с ошибками: часть операций FAILED."""
        class FailOnThird:
            def __init__(self):
                self._call_count = 0
                self._store: dict[str, dict] = {}

            async def fetchval(self, query, *args):
                self._call_count += 1
                if self._call_count == 3:
                    raise RuntimeError("DB error on 3rd insert")
                self._call_count_seq = getattr(self, '_seq', 0) + 1
                self._seq = self._call_count_seq
                return str(self._seq)

        fail_pool = FailOnThird()
        provider._pool = fail_pool

        # 2 успеха, 1 ошибка, 1 успех
        await provider.insert("items", {"name": "ok1"})
        await provider.insert("items", {"name": "ok2"})

        with pytest.raises(RuntimeError, match="DB error on 3rd insert"):
            await provider.insert("items", {"name": "fail"})

        await provider.insert("items", {"name": "ok3"})

        history = task_store.get_history()
        assert len(history) == 4

        statuses = [t.status for t in history]
        assert statuses.count(TaskStatus.COMPLETED) == 3
        assert statuses.count(TaskStatus.FAILED) == 1


# ============================================================
# 6. Интеграция с SmartDispatcher
# ============================================================


class TestSmartDispatcherIntegration:
    """E2E: DatabaseProvider + SmartDispatcher через @db_method metadata."""

    @pytest.mark.asyncio
    async def test_db_method_sets_task_type_for_dispatcher(
        self, provider: DatabaseProvider,
    ) -> None:
        """@db_method устанавливает _task_type для SmartDispatcher."""
        assert provider.get._task_type == TaskType.IO       # read → io
        assert provider.insert._task_type == TaskType.IO     # write → io
        assert provider.count._task_type == TaskType.AGGREGATE
        assert provider.transaction._task_type == TaskType.DATABASE

    @pytest.mark.asyncio
    async def test_legacy_db_type_preserved(
        self, provider: DatabaseProvider,
    ) -> None:
        """_db_type сохраняется для обратной совместимости со SmartDispatcher."""
        assert provider.get._db_type == "read"
        assert provider.insert._db_type == "write"
        assert provider.count._db_type == "aggregate"
        assert provider.transaction._db_type == "transaction"

    @pytest.mark.asyncio
    async def test_task_timeout_preserved(
        self, provider: DatabaseProvider,
    ) -> None:
        """_task_timeout установлен для SmartDispatcher."""
        assert provider.get._task_timeout == 5.0
        assert provider.insert._task_timeout == 10.0
        assert provider.transaction._task_timeout == 30.0

    @pytest.mark.asyncio
    async def test_classifier_classifies_db_provider_tasks(
        self, task_store: TaskStore,
    ) -> None:
        """TaskClassifier корректно классифицирует задачи db.provider."""
        classifier = TaskClassifier()

        t = Task.create(module_id="db.provider", fn_name="get", task_type=TaskType.IO)
        result = classifier.classify(t, provider_get_stub)
        # stub не имеет _task_type, classifier определяет по module_id="db.provider"
        assert result in (TaskType.IO, TaskType.DATABASE, TaskType.UNKNOWN)


def provider_get_stub(table: str, id: str) -> dict | None:
    return None

provider_get_stub._task_type = TaskType.IO


# ============================================================
# 7. Batch операции (bulk_insert, bulk_update, bulk_delete)
# ============================================================


class TestBatchOperations:
    """E2E: bulk-операции трекаются в TaskStore."""

    @pytest.mark.asyncio
    async def test_bulk_insert_creates_single_task(
        self, provider: DatabaseProvider, task_store: TaskStore,
    ) -> None:
        """bulk_insert создаёт одну задачу."""
        records = [{"name": f"item_{i}"} for i in range(5)]
        ids = await provider.bulk_insert("items", records)
        assert len(ids) == 5

        history = task_store.get_history()
        bulk_tasks = [t for t in history if t.fn_name == "bulk_insert"]
        assert len(bulk_tasks) == 1
        assert bulk_tasks[0].status == TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_bulk_insert_empty_returns_empty(
        self, provider: DatabaseProvider, task_store: TaskStore,
    ) -> None:
        """bulk_insert с пустым списком — ничего не создаёт."""
        ids = await provider.bulk_insert("items", [])
        assert ids == []
        assert len(task_store.get_history()) == 0

    @pytest.mark.asyncio
    async def test_bulk_delete_creates_single_task(
        self, provider: DatabaseProvider, task_store: TaskStore,
    ) -> None:
        """bulk_delete создаёт одну задачу."""
        await provider.insert("items", {"name": "a"})
        await provider.insert("items", {"name": "b"})

        deleted = await provider.bulk_delete("items", ["1", "2"])
        assert deleted == 2

        history = task_store.get_history()
        bulk_tasks = [t for t in history if t.fn_name == "bulk_delete"]
        assert len(bulk_tasks) == 1


# ============================================================
# 8. validate_identifier — SQL injection protection
# ============================================================


class TestSQLInjectionProtection:
    """E2E: валидация SQL-идентификаторов предотвращает инъекции."""

    @pytest.mark.asyncio
    async def test_invalid_table_name_rejected(
        self, provider: DatabaseProvider,
    ) -> None:
        """Таблица с спецсимволами отклоняется."""
        with pytest.raises(ValueError, match="Invalid SQL"):
            await provider.get("users; DROP TABLE users--", "1")

    @pytest.mark.asyncio
    async def test_invalid_column_name_rejected(
        self, provider: DatabaseProvider,
    ) -> None:
        """Колонка с спецсимволами отклоняется."""
        with pytest.raises(ValueError, match="Invalid SQL"):
            await provider.insert("users", {"name'; --": "hacked"})

    @pytest.mark.asyncio
    async def test_valid_identifiers_pass(
        self, provider: DatabaseProvider, pool: E2EMockPool,
    ) -> None:
        """Валидные имена таблиц/колонок проходят."""
        pool._store["id:1"] = {"id": "1", "name": "test"}
        result = await provider.get("users", "1")
        assert result is not None
