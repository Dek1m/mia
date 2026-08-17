"""Тесты DatabaseProvider v2 — CRUD операции и @db_method."""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.task import TaskStatus, TaskType
from modules.db.provider import DatabaseProvider, db_method, validate_identifier


# ── Мок-пул ────────────────────────────────────────────


class MockPool:
    """Минимальный мок asyncpg pool."""

    def __init__(self) -> None:
        self._store: dict[str, dict] = {}
        self._seq = 0

    async def fetchrow(self, query: str, *args: Any) -> dict | None:
        if "UPDATE" in query and "RETURNING" in query:
            for record in self._store.values():
                if args and record.get("id") == args[-1]:
                    for i, key in enumerate(["name"]):
                        if i < len(args) - 1:
                            record[key] = args[i]
                    return dict(record)
            return None
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
def provider(pool: MockPool, config: Any) -> DatabaseProvider:
    return DatabaseProvider(pool=pool, config=config)


# ── 1. CRUD-операции ──────────────────────────────────


class TestCRUDOperations:
    """DatabaseProvider CRUD-методы работают корректно."""

    @pytest.mark.asyncio
    async def test_get(self, provider: DatabaseProvider, pool: MockPool) -> None:
        pool._store["id:1"] = {"id": "1", "name": "Alice"}
        result = await provider.get("users", "1")
        assert result == {"id": "1", "name": "Alice"}

    @pytest.mark.asyncio
    async def test_insert(self, provider: DatabaseProvider) -> None:
        result = await provider.insert("users", {"name": "Alice"})
        assert result == "1"

    @pytest.mark.asyncio
    async def test_update(self, provider: DatabaseProvider, pool: MockPool) -> None:
        pool._store["id:1"] = {"id": "1", "name": "Alice"}
        result = await provider.update("users", "1", {"name": "Bob"})
        assert result == {"id": "1", "name": "Bob"}

    @pytest.mark.asyncio
    async def test_delete(self, provider: DatabaseProvider, pool: MockPool) -> None:
        pool._store["id:1"] = {"id": "1", "name": "Alice"}
        result = await provider.delete("users", "1")
        assert result is True

    @pytest.mark.asyncio
    async def test_exists(self, provider: DatabaseProvider, pool: MockPool) -> None:
        pool._store["id:1"] = {"id": "1", "name": "Alice"}
        result = await provider.exists("users", "1")
        assert result is True

    @pytest.mark.asyncio
    async def test_count(self, provider: DatabaseProvider, pool: MockPool) -> None:
        pool._store["id:1"] = {"id": "1", "name": "Alice"}
        pool._store["id:2"] = {"id": "2", "name": "Bob"}
        result = await provider.count("users")
        assert result == 2


# ── 2. Метаданные декоратора ────────────────────────────


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


# ── 3. Setter-методы ───────────────────────────────────


class TestSetterMethods:
    """set_stats_writer и set_dispatcher корректно устанавливают зависимости."""

    def test_set_stats_writer(self, provider: DatabaseProvider) -> None:
        writer = MagicMock()
        provider.set_stats_writer(writer)
        assert provider._stats_writer is writer

    def test_set_dispatcher(self, provider: DatabaseProvider) -> None:
        dispatcher = MagicMock()
        provider.set_dispatcher(dispatcher)
        assert provider._dispatcher is dispatcher
