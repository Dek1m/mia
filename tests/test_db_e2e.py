"""E2E-тесты модуля db — sync CRUD на psycopg v3."""
from __future__ import annotations

import threading
from typing import Any
from unittest.mock import MagicMock

import pytest

from core.stats_batch_writer import StatsBatchWriter
from modules.db.provider import DatabaseProvider
from modules.db.config import DatabaseConfig


# ── Мок-пул (psycopg v3 style) ─────────────────────


class E2ECursor:
    """Полнофункциональный мок cursor psycopg v3."""

    def __init__(self, store: dict[str, dict], lock: threading.Lock, seq_counter: list[int] | None = None) -> None:
        self._store = store
        self._lock = lock
        self._seq_ref = seq_counter if seq_counter is not None else [0]
        self.description: list[tuple] = []
        self.rowcount: int = 0
        self.statusmessage: str = "OK"
        self._result: Any = None

    def execute(self, query: str, params: tuple = ()) -> None:
        with self._lock:
            self.rowcount = 0
            self._result = None

            if "INSERT" in query and "RETURNING id" in query:
                self._seq_ref[0] += 1
                id_ = str(self._seq_ref[0])
                cols_part = query.split("(")[1].split(")")[0]
                columns = [c.strip() for c in cols_part.split(",")]
                data = dict(zip(columns, params))
                data["id"] = id_
                self._store[f"id:{id_}"] = data
                self.statusmessage = "INSERT 0 1"
                self.description = [("id",)]
                self._result = (id_,)
            elif "SELECT EXISTS" in query:
                target_id = params[0] if params else None
                found = any(r.get("id") == target_id for r in self._store.values())
                self.description = [("exists",)]
                self._result = (found,)
            elif "SELECT COUNT" in query:
                self.description = [("count",)]
                self._result = (len(self._store),)
            elif "SELECT" in query and "FROM" in query:
                target_id = params[0] if params else None
                for record in self._store.values():
                    if record.get("id") == target_id:
                        self.description = [(k,) for k in record.keys()]
                        self._result = tuple(record.values())
                        return
                self._result = None
            elif "UPDATE" in query and "RETURNING" in query:
                target_id = params[-1] if params else None
                for record in self._store.values():
                    if record.get("id") == target_id:
                        for i, val in enumerate(params[:-1]):
                            keys = [k for k in record.keys() if k != "id"]
                            if i < len(keys):
                                record[keys[i]] = val
                        self.description = [(k,) for k in record.keys()]
                        self._result = tuple(record.values())
                        self.statusmessage = "UPDATE 1"
                        return
                self._result = None
            elif "DELETE" in query and "ANY" in query:
                ids_to_delete = params[0] if params else []
                deleted = 0
                for record in list(self._store.values()):
                    if record.get("id") in ids_to_delete:
                        del self._store[f"id:{record['id']}"]
                        deleted += 1
                self.rowcount = deleted
                self.statusmessage = f"DELETE {deleted}"
            elif "DELETE" in query:
                target_id = params[0] if params else None
                for key, record in list(self._store.items()):
                    if record.get("id") == target_id:
                        del self._store[key]
                        self.rowcount = 1
                        self.statusmessage = "DELETE 1"
                        return
                self.statusmessage = "DELETE 0"
            elif "UPDATE" in query:
                target_id = params[-1] if params else None
                for record in self._store.values():
                    if record.get("id") == target_id:
                        for i, val in enumerate(params[:-1]):
                            keys = [k for k in record.keys() if k != "id"]
                            if i < len(keys):
                                record[keys[i]] = val
                        self.statusmessage = "UPDATE 1"
                        return
                self.statusmessage = "UPDATE 0"
            elif "INSERT" in query:
                self._seq_ref[0] += 1
                id_ = str(self._seq_ref[0])
                data = {"id": id_}
                if params:
                    data["name"] = params[0]
                self._store[f"id:{id_}"] = data
                self.statusmessage = "INSERT 0 1"
            else:
                self.statusmessage = "OK"

    def fetchone(self) -> tuple | None:
        return self._result

    def fetchall(self) -> list[tuple]:
        return [self._result] if self._result else []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class E2EConnection:
    """Мок connection psycopg v3."""

    def __init__(self, store: dict[str, dict], lock: threading.Lock, seq_counter: list[int]) -> None:
        self._store = store
        self._lock = lock
        self._seq_counter = seq_counter

    def cursor(self) -> E2ECursor:
        return E2ECursor(self._store, self._lock, self._seq_counter)

    def execute(self, query: str, params: tuple = ()) -> None:
        cursor = E2ECursor(self._store, self._lock)
        cursor.execute(query, params)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class E2EMockPool:
    """Полнофункциональный мок psycopg_pool.ConnectionPool."""

    def __init__(self) -> None:
        self._store: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._seq_counter: list[int] = [0]  # общий счётчик для всех cursors

    def connection(self) -> E2EConnection:
        return E2EConnection(self._store, self._lock, self._seq_counter)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


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

    def test_insert_get_update_delete(
        self, provider: DatabaseProvider,
    ) -> None:
        """Полный CRUD: insert → get → update → delete."""
        id1 = provider.insert("users", {"name": "Alice"})
        assert id1 == "1"

        user = provider.get("users", id1)
        assert user == {"id": "1", "name": "Alice"}

        updated = provider.update("users", id1, {"name": "Bob"})
        assert updated is not None

        deleted = provider.delete("users", id1)
        assert deleted is True

    def test_error_propagation(
        self, provider: DatabaseProvider,
    ) -> None:
        """Ошибка в пуле пробрасывается."""
        class BrokenPool:
            def connection(self):
                raise RuntimeError("DB connection lost")

        provider._pool = BrokenPool()

        with pytest.raises(RuntimeError, match="DB connection lost"):
            provider.get("users", "1")


# ============================================================
# 2. Batch-операции
# ============================================================


class TestBatchOperations:
    """E2E: bulk-операции работают через DatabaseProvider."""

    def test_bulk_insert(
        self, provider: DatabaseProvider,
    ) -> None:
        """bulk_insert вставляет несколько записей."""
        ids = provider.bulk_insert("users", [
            {"name": "Alice"},
            {"name": "Bob"},
        ])
        assert len(ids) == 2

    def test_bulk_delete(
        self, provider: DatabaseProvider,
    ) -> None:
        """bulk_delete удаляет несколько записей."""
        id1 = provider.insert("users", {"name": "Alice"})
        id2 = provider.insert("users", {"name": "Bob"})

        deleted = provider.bulk_delete("users", [id1, id2])
        assert deleted == 2

    def test_bulk_insert_empty(
        self, provider: DatabaseProvider,
    ) -> None:
        """bulk_insert с пустым списком."""
        ids = provider.bulk_insert("users", [])
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
