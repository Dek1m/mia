"""Тесты Database v2 — CRUD операции и обратная совместимость."""
from __future__ import annotations

import pytest
from typing import Any
from unittest.mock import MagicMock

from core.database import Database


# ── Мок-провайдер ──────────────────────────────────────


class InMemoryProvider:
    """Провайдер, хранящий данные в памяти."""

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
def provider() -> InMemoryProvider:
    return InMemoryProvider()


@pytest.fixture
def db(provider: InMemoryProvider) -> Database:
    """Database без Task System."""
    db = Database(cache=None, dispatcher=None)
    db.register_provider("mem", provider, is_default=True)
    return db


# ── 1. CRUD-операции ──────────────────────────────────


class TestCRUDOperations:
    """Database facade корректно делегирует CRUD провайдеру."""

    def test_get(self, db: Database, provider: InMemoryProvider) -> None:
        provider._store["users:1"] = {"id": "1", "name": "Alice"}
        result = db.get("users", "1")
        assert result == {"id": "1", "name": "Alice"}

    def test_insert(self, db: Database) -> None:
        result = db.insert("users", {"name": "Bob"})
        assert result == "1"

    def test_update(self, db: Database, provider: InMemoryProvider) -> None:
        provider._store["users:1"] = {"id": "1", "name": "Alice"}
        result = db.update("users", "1", {"name": "Bob"})
        assert result == {"id": "1", "name": "Bob"}

    def test_delete(self, db: Database, provider: InMemoryProvider) -> None:
        provider._store["users:1"] = {"id": "1"}
        result = db.delete("users", "1")
        assert result is True

    def test_crud_full_cycle(self, db: Database) -> None:
        id1 = db.insert("users", {"name": "Alice"})
        assert db.get("users", id1) is not None
        assert db.update("users", id1, {"name": "Bob"}) is not None
        assert db.delete("users", id1) is True

    def test_error_propagation(self, db: Database) -> None:
        class BrokenProvider:
            def get(self, table, id):
                raise RuntimeError("DB down")
            def insert(self, table, data):
                return "1"

        db.register_provider("broken", BrokenProvider(), is_default=True)
        with pytest.raises(RuntimeError, match="DB down"):
            db.get("t", "1")


# ── 2. Обратная совместимость ──────────────────────────


class TestBackwardCompatibility:
    """Database без Task System работает как раньше."""

    def test_no_task_store_no_errors(self, db: Database) -> None:
        """Если task_store=None — ошибок нет."""
        assert db._stats_writer is None
        id_ = db.insert("t", {"v": 1})
        assert db.get("t", id_) == {"id": "1", "v": 1}
        assert db.update("t", id_, {"v": 2}) == {"id": "1", "v": 2}
        assert db.delete("t", id_) is True


# ── 3. Factory тест ────────────────────────────────────


class TestFactory:
    """DatabaseFactory.create_with_task_system корректно создаёт все компоненты."""

    def test_create_with_task_system(self) -> None:
        from core.factories import DatabaseFactory

        database, task_store, stats_writer = DatabaseFactory.create_with_task_system()

        assert isinstance(database, Database)
        assert task_store is None
        assert database._stats_writer is stats_writer
        assert stats_writer._db is database

    def test_create_without_task_system(self) -> None:
        from core.factories import DatabaseFactory

        database = DatabaseFactory.create()

        assert isinstance(database, Database)
        assert database._stats_writer is None
