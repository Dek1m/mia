"""E2E-тесты модуля db — CRUD, кеш, lock, retry, batch-операции."""
from __future__ import annotations

import asyncio
import threading
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.stats_batch_writer import StatsBatchWriter
from pools.smart_dispatcher import SmartDispatcher
from modules.db.provider import DatabaseProvider, db_method, _resolve_cache_key
from modules.db.config import DatabaseConfig


# ── Мок-пул ────────────────────────────────────────────


class E2EMockPool:
    """Полнофункциональный мок asyncpg pool."""

    def __init__(self) -> None:
        self._store: dict[str, dict] = {}
        self._seq = 0
        self._lock = threading.Lock()

    async def fetchrow(self, query: str, *args: Any) -> dict | None:
        with self._lock:
            if "UPDATE" in query and "RETURNING" in query:
                for record in self._store.values():
                    if args and record.get("id") == args[-1]:
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
                data = {"id": id_}
                if args:
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
                        for i, key in enumerate(["name"]):
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
def stats_writer() -> MagicMock:
    return MagicMock(spec=StatsBatchWriter)


@pytest.fixture
def provider(pool: E2EMockPool, config: DatabaseConfig) -> DatabaseProvider:
    return DatabaseProvider(pool=pool, config=config)


@pytest.fixture
def cache() -> E2ECache:
    return E2ECache()


@pytest.fixture
def provider_with_cache(pool: E2EMockPool, config: DatabaseConfig, cache: E2ECache) -> DatabaseProvider:
    p = DatabaseProvider(pool=pool, config=config)
    p.set_cache(cache)
    return p


# ============================================================
# 1. CRUD-операции
# ============================================================


class TestFullCRUDCycle:
    """E2E: полный CRUD-цикл через DatabaseProvider."""

    @pytest.mark.asyncio
    async def test_insert_get_update_delete(
        self, provider: DatabaseProvider,
    ) -> None:
        """Полный CRUD: insert → get → update → delete."""
        id1 = await provider.insert("users", {"name": "Alice"})
        assert id1 == "1"

        user = await provider.get("users", id1)
        assert user == {"id": "1", "name": "Alice"}

        updated = await provider.update("users", id1, {"name": "Bob"})
        assert updated is not None

        deleted = await provider.delete("users", id1)
        assert deleted is True

    @pytest.mark.asyncio
    async def test_error_propagation(
        self, provider: DatabaseProvider,
    ) -> None:
        """Ошибка в пуле пробрасывается."""
        class BrokenPool:
            async def fetchrow(self, query, *args):
                raise RuntimeError("DB connection lost")

        provider._pool = BrokenPool()

        with pytest.raises(RuntimeError, match="DB connection lost"):
            await provider.get("users", "1")


# ============================================================
# 2. Кеш через @db_method
# ============================================================


class TestCacheViaDbMethod:
    """E2E: кеш работает через декоратор @db_method."""

    @pytest.mark.asyncio
    async def test_cache_hit_returns_cached_value(
        self, provider_with_cache: DatabaseProvider, pool: E2EMockPool, cache: E2ECache,
    ) -> None:
        """Второй get возвращает кеш."""
        pool._store["id:1"] = {"id": "1", "name": "Alice"}

        result1 = await provider_with_cache.get("users", "1")
        assert result1 == {"id": "1", "name": "Alice"}
        assert cache.hits == 0
        assert cache.misses == 1

        result2 = await provider_with_cache.get("users", "1")
        assert result2 == {"id": "1", "name": "Alice"}
        assert cache.hits == 1

    @pytest.mark.asyncio
    async def test_cache_disabled_when_ttl_zero(
        self, provider_with_cache: DatabaseProvider, pool: E2EMockPool, cache: E2ECache,
    ) -> None:
        """cache_ttl=0 → кеш не используется."""
        pool._store["id:1"] = {"id": "1", "name": "Alice"}

        await provider_with_cache.exists("users", "1")
        assert cache.hits == 0
        assert cache.misses == 0


# ============================================================
# 3. Lock через @db_method
# ============================================================


class TestLockViaDbMethod:
    """E2E: lock работает через декоратор @db_method."""

    @pytest.mark.asyncio
    async def test_write_lock_attribute(
        self, provider: DatabaseProvider,
    ) -> None:
        """write-метод имеет _db_lock."""
        assert hasattr(provider.insert, "_db_lock")

    @pytest.mark.asyncio
    async def test_read_has_no_lock(
        self, provider: DatabaseProvider,
    ) -> None:
        """read-метод не имеет lock."""
        assert getattr(provider.get, "_db_lock") is None


# ============================================================
# 4. Retry через @task
# ============================================================


class TestRetryViaTask:
    """E2E: retry работает через @task decorator."""

    @pytest.mark.asyncio
    async def test_retry_metadata(
        self, provider: DatabaseProvider,
    ) -> None:
        """@db_method сохраняет retry-метаданные."""
        assert getattr(provider.get, "_db_retry") == 0

    @pytest.mark.asyncio
    async def test_timeout_metadata(
        self, provider: DatabaseProvider,
    ) -> None:
        """@db_method сохраняет timeout-метаданные."""
        assert getattr(provider.get, "_db_timeout") == 5.0


# ============================================================
# 5. Batch-операции
# ============================================================


class TestBatchOperations:
    """E2E: bulk-операции работают через DatabaseProvider."""

    @pytest.mark.asyncio
    async def test_bulk_insert(
        self, provider: DatabaseProvider,
    ) -> None:
        """bulk_insert вставляет несколько записей."""
        ids = await provider.bulk_insert("users", [
            {"name": "Alice"},
            {"name": "Bob"},
        ])
        assert len(ids) == 2

    @pytest.mark.asyncio
    async def test_bulk_delete(
        self, provider: DatabaseProvider,
    ) -> None:
        """bulk_delete удаляет несколько записей."""
        id1 = await provider.insert("users", {"name": "Alice"})
        id2 = await provider.insert("users", {"name": "Bob"})

        deleted = await provider.bulk_delete("users", [id1, id2])
        assert deleted == 2

    @pytest.mark.asyncio
    async def test_bulk_insert_empty(
        self, provider: DatabaseProvider,
    ) -> None:
        """bulk_insert с пустым списком."""
        ids = await provider.bulk_insert("users", [])
        assert ids == []


# ============================================================
# 6. SQL Injection Protection
# ============================================================


class TestSQLInjectionProtection:
    """E2E: валидация SQL-идентификаторов."""

    def test_invalid_table_name(self) -> None:
        """Невалидное имя таблицы → ValueError."""
        from modules.db.provider import validate_identifier
        with pytest.raises(ValueError):
            validate_identifier("users; DROP TABLE")

    def test_valid_table_name(self) -> None:
        """Валидное имя таблицы → OK."""
        from modules.db.provider import validate_identifier
        assert validate_identifier("users") == "users"

    def test_invalid_order_by(self) -> None:
        """Невалидный ORDER BY → ValueError."""
        from modules.db.provider import validate_order_by
        with pytest.raises(ValueError):
            validate_order_by("name; DROP TABLE")
