"""Тесты DatabaseProvider v2 — интеграция с Universal Task System."""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.task import TaskStatus, TaskType
from core.task_store import TaskStore
from core.stats_batch_writer import StatsBatchWriter
from modules.db.provider import DatabaseProvider, db_method, validate_identifier


# ── Мок-пул ────────────────────────────────────────────


class MockPool:
    """Минимальный мок asyncpg pool."""

    def __init__(self) -> None:
        self._store: dict[str, dict] = {}
        self._seq = 0

    async def fetchrow(self, query: str, *args: Any) -> dict | None:
        # Простая имитация SELECT
        if "SELECT" in query and "FROM" in query:
            for key, record in self._store.items():
                if len(args) > 0 and record.get("id") == args[0]:
                    return record
            return None
        return None

    async def fetchval(self, query: str, *args: Any) -> Any:
        if "INSERT" in query:
            self._seq += 1
            id_ = str(self._seq)
            # Парсим колонки из query (упрощённо)
            self._store[f"id:{id_}"] = {"id": id_, **dict(zip(["name"], args))}
            return id_
        if "SELECT EXISTS" in query:
            for record in self._store.values():
                if len(args) > 0 and record.get("id") == args[0]:
                    return True
            return False
        if "SELECT COUNT" in query:
            return len(self._store)
        return None

    async def fetch(self, query: str, *args: Any) -> list[dict]:
        return list(self._store.values())

    async def execute(self, query: str, *args: Any) -> str:
        if "DELETE" in query:
            for record in list(self._store.values()):
                if len(args) > 0 and record.get("id") == args[0]:
                    del self._store[f"id:{record['id']}"]
                    return "DELETE 1"
            return "DELETE 0"
        return "OK"


# ── Фикстуры ───────────────────────────────────────────


@pytest.fixture
def pool() -> MockPool:
    return MockPool()


@pytest.fixture
def config() -> Any:
    from modules.db.config import DatabaseConfig
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
def provider_with_tasks(
    pool: MockPool, config: Any, task_store: TaskStore, stats_writer: MagicMock,
) -> DatabaseProvider:
    return DatabaseProvider(
        pool=pool,
        config=config,
        task_store=task_store,
        stats_writer=stats_writer,
    )


@pytest.fixture
def provider_without_tasks(pool: MockPool, config: Any) -> DatabaseProvider:
    return DatabaseProvider(pool=pool, config=config)


# ── 1. TaskStore интеграция ────────────────────────────


class TestTaskStoreIntegration:
    """TaskStore получает задачи при каждой CRUD-операции."""

    @pytest.mark.asyncio
    async def test_get_records_task(
        self, provider_with_tasks: DatabaseProvider, task_store: TaskStore,
    ) -> None:
        result = await provider_with_tasks.get("users", "1")

        history = task_store.get_history()
        assert len(history) == 1
        task = history[0]
        assert task.fn_name == "get"
        assert task.module_id == "db.provider"
        assert task.task_type == TaskType.IO
        assert task.status == TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_insert_records_task(
        self, provider_with_tasks: DatabaseProvider, task_store: TaskStore,
    ) -> None:
        result = await provider_with_tasks.insert("users", {"name": "Alice"})

        history = task_store.get_history()
        assert len(history) == 1
        task = history[0]
        assert task.fn_name == "insert"
        assert task.status == TaskStatus.COMPLETED
        assert task.result == result

    @pytest.mark.asyncio
    async def test_update_records_task(
        self, provider_with_tasks: DatabaseProvider, task_store: TaskStore,
    ) -> None:
        await provider_with_tasks.insert("users", {"name": "Alice"})
        result = await provider_with_tasks.update("users", "1", {"name": "Bob"})

        history = task_store.get_history()
        assert len(history) == 2  # insert + update
        update_task = history[0]
        assert update_task.fn_name == "update"
        assert update_task.status == TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_delete_records_task(
        self, provider_with_tasks: DatabaseProvider, task_store: TaskStore,
    ) -> None:
        await provider_with_tasks.insert("users", {"name": "Alice"})
        result = await provider_with_tasks.delete("users", "1")

        history = task_store.get_history()
        assert len(history) == 2  # insert + delete
        delete_task = history[0]
        assert delete_task.fn_name == "delete"
        assert delete_task.status == TaskStatus.COMPLETED
        assert delete_task.result is True

    @pytest.mark.asyncio
    async def test_error_records_failed_task(
        self, provider_with_tasks: DatabaseProvider, task_store: TaskStore,
    ) -> None:
        """При ошибке — task в истории со статусом FAILED."""
        class BadPool:
            async def fetchrow(self, query, *args):
                raise RuntimeError("Connection lost")

        provider_with_tasks._pool = BadPool()

        with pytest.raises(RuntimeError, match="Connection lost"):
            await provider_with_tasks.get("users", "1")

        history = task_store.get_history()
        assert len(history) == 1
        task = history[0]
        assert task.fn_name == "get"
        assert task.status == TaskStatus.FAILED
        assert "Connection lost" in task.error

    @pytest.mark.asyncio
    async def test_multiple_operations_create_multiple_tasks(
        self, provider_with_tasks: DatabaseProvider, task_store: TaskStore,
    ) -> None:
        id1 = await provider_with_tasks.insert("users", {"name": "Alice"})
        await provider_with_tasks.get("users", id1)
        await provider_with_tasks.update("users", id1, {"name": "Bob"})
        await provider_with_tasks.delete("users", id1)

        history = task_store.get_history()
        assert len(history) == 4
        fn_names = [t.fn_name for t in history]
        assert fn_names == ["delete", "update", "get", "insert"]


# ── 2. StatsWriter интеграция ──────────────────────────


class TestStatsWriterIntegration:
    """StatsBatchWriter.add() вызывается после каждой операции."""

    @pytest.mark.asyncio
    async def test_stats_writer_receives_task(
        self,
        provider_with_tasks: DatabaseProvider,
        stats_writer: MagicMock,
    ) -> None:
        await provider_with_tasks.get("users", "1")

        stats_writer.add.assert_called_once()
        task_arg = stats_writer.add.call_args[0][0]
        assert task_arg.fn_name == "get"

    @pytest.mark.asyncio
    async def test_stats_writer_receives_on_error(
        self,
        provider_with_tasks: DatabaseProvider,
        stats_writer: MagicMock,
    ) -> None:
        class BadPool:
            async def fetchval(self, query, *args):
                raise ValueError("fail")

        provider_with_tasks._pool = BadPool()

        with pytest.raises(ValueError):
            await provider_with_tasks.exists("users", "1")

        stats_writer.add.assert_called_once()
        task_arg = stats_writer.add.call_args[0][0]
        assert task_arg.status == TaskStatus.FAILED


# ── 3. Обратная совместимость ──────────────────────────


class TestBackwardCompatibility:
    """DatabaseProvider без Task System работает как раньше."""

    @pytest.mark.asyncio
    async def test_get_without_task_store(
        self, provider_without_tasks: DatabaseProvider, pool: MockPool,
    ) -> None:
        pool._store["id:1"] = {"id": "1", "name": "Alice"}
        result = await provider_without_tasks.get("users", "1")
        assert result == {"id": "1", "name": "Alice"}

    @pytest.mark.asyncio
    async def test_insert_without_task_store(
        self, provider_without_tasks: DatabaseProvider,
    ) -> None:
        result = await provider_without_tasks.insert("users", {"name": "Bob"})
        assert result == "1"

    @pytest.mark.asyncio
    async def test_no_task_store_no_errors(
        self, provider_without_tasks: DatabaseProvider,
    ) -> None:
        """Если task_store=None — задачи не создаются, ошибок нет."""
        assert provider_without_tasks._task_store is None
        assert provider_without_tasks._stats_writer is None

        id_ = await provider_without_tasks.insert("t", {"v": 1})
        assert await provider_without_tasks.get("t", id_) is not None


# ── 4. Метаданные декоратора ────────────────────────────


class TestDecoratorMetadata:
    """Атрибуты @db_method сохраняются на wrapper."""

    def test_metadata_preserved(self) -> None:
        @db_method(
            type="read",
            timeout=5.0,
            cache_ttl=30,
            cache_key="item:{id}",
            lock="lock:{id}",
            retry=2,
            retry_delay=0.1,
            metrics="custom.metric",
        )
        async def get_item(self, id: str) -> dict:
            return {"id": id}

        assert get_item._db_type == "read"
        assert get_item._db_timeout == 5.0
        assert get_item._db_cache_ttl == 30
        assert get_item._db_cache_key == "item:{id}"
        assert get_item._db_lock == "lock:{id}"
        assert get_item._db_retry == 2
        assert get_item._db_retry_delay == 0.1
        assert get_item._db_metrics == "custom.metric"

    def test_default_metrics(self) -> None:
        @db_method()
        async def my_method(self) -> None:
            pass

        assert my_method._db_metrics == "db.my_method"


# ── 5. set_task_store / set_stats_writer ────────────────


class TestSetterMethods:
    """set_task_store и set_stats_writer корректно устанавливают зависимости."""

    def test_set_task_store(self, provider_without_tasks: DatabaseProvider) -> None:
        store = TaskStore()
        provider_without_tasks.set_task_store(store)
        assert provider_without_tasks._task_store is store

    def test_set_stats_writer(self, provider_without_tasks: DatabaseProvider) -> None:
        writer = MagicMock(spec=StatsBatchWriter)
        provider_without_tasks.set_stats_writer(writer)
        assert provider_without_tasks._stats_writer is writer
