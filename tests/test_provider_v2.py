"""Тесты DatabaseProvider v2 — sync CRUD на psycopg v3."""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from modules.db.provider import DatabaseProvider, validate_identifier


# ── Мок-пул (psycopg v3 style) ─────────────────────


class MockCursor:
    """Мок cursor psycopg v3."""

    def __init__(self, store: dict[str, dict]) -> None:
        self._store = store
        self._seq = 0
        self.description: list[tuple] = []
        self.rowcount: int = 0
        self.statusmessage: str = "OK"
        self._last_query: str = ""
        self._last_args: tuple = ()

    def execute(self, query: str, params: tuple = ()) -> None:
        self._last_query = query
        self._last_args = params
        self.rowcount = 0

        if "INSERT" in query and "RETURNING id" in query:
            self._seq += 1
            id_ = str(self._seq)
            # Парсим колонки из INSERT INTO table (col1, col2)
            cols_part = query.split("(")[1].split(")")[0]
            columns = [c.strip() for c in cols_part.split(",")]
            data = dict(zip(columns, params))
            data["id"] = id_
            self._store[f"id:{id_}"] = data
            self.statusmessage = f"INSERT 0 1"
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
                        # Простая подстановка по порядку
                        keys = [k for k in record.keys() if k != "id"]
                        if i < len(keys):
                            record[keys[i]] = val
                    self.description = [(k,) for k in record.keys()]
                    self._result = tuple(record.values())
                    self.statusmessage = "UPDATE 1"
                    return
            self._result = None
        elif "DELETE" in query:
            target_id = params[0] if params else None
            for key, record in list(self._store.items()):
                if record.get("id") == target_id:
                    del self._store[key]
                    self.rowcount = 1
                    self.statusmessage = "DELETE 1"
                    return
            self.statusmessage = "DELETE 0"
        else:
            self._result = None

    def fetchone(self) -> tuple | None:
        return getattr(self, "_result", None)

    def fetchall(self) -> list[tuple]:
        return [getattr(self, "_result", None)] if hasattr(self, "_result") and self._result else []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class MockConnection:
    """Мок connection psycopg v3."""

    def __init__(self, store: dict[str, dict]) -> None:
        self._store = store

    def cursor(self) -> MockCursor:
        return MockCursor(self._store)

    def execute(self, query: str, params: tuple = ()) -> None:
        cursor = MockCursor(self._store)
        cursor.execute(query, params)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class MockPool:
    """Мок psycopg_pool.ConnectionPool."""

    def __init__(self) -> None:
        self._store: dict[str, dict] = {}

    def connection(self) -> MockConnection:
        return MockConnection(self._store)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


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

    def test_get(self, provider: DatabaseProvider, pool: MockPool) -> None:
        pool._store["id:1"] = {"id": "1", "name": "Alice"}
        result = provider.get("users", "1")
        assert result == {"id": "1", "name": "Alice"}

    def test_insert(self, provider: DatabaseProvider) -> None:
        result = provider.insert("users", {"name": "Alice"})
        assert result == "1"

    def test_update(self, provider: DatabaseProvider, pool: MockPool) -> None:
        pool._store["id:1"] = {"id": "1", "name": "Alice"}
        result = provider.update("users", "1", {"name": "Bob"})
        assert result == {"id": "1", "name": "Bob"}

    def test_delete(self, provider: DatabaseProvider, pool: MockPool) -> None:
        pool._store["id:1"] = {"id": "1", "name": "Alice"}
        result = provider.delete("users", "1")
        assert result is True

    def test_exists(self, provider: DatabaseProvider, pool: MockPool) -> None:
        pool._store["id:1"] = {"id": "1", "name": "Alice"}
        result = provider.exists("users", "1")
        assert result is True

    def test_count(self, provider: DatabaseProvider, pool: MockPool) -> None:
        pool._store["id:1"] = {"id": "1", "name": "Alice"}
        pool._store["id:2"] = {"id": "2", "name": "Bob"}
        result = provider.count("users")
        assert result == 2


# ── 2. Setter-методы ──────────────────────────────────


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
