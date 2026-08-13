"""Тесты для Database фасада."""
from __future__ import annotations

import pytest
from core.database import Database
from core.interfaces import IDatabase


class MockProvider:
    """Мок-провайдер для тестов."""

    def get(self, table: str, id: str) -> dict | None:
        return {"id": id, "table": table}

    def get_by_field(self, table: str, field: str, value) -> dict | None:
        return {"table": table, field: value}

    def insert(self, table: str, data: dict) -> str:
        return "new-id"

    def update(self, table: str, id: str, data: dict) -> dict | None:
        return {"id": id, **data}

    def delete(self, table: str, id: str) -> bool:
        return True

    def exists(self, table: str, id: str) -> bool:
        return True

    def count(self, table: str, filters=None) -> int:
        return 10

    def list(self, table: str, filters=None, limit=100, offset=0) -> list[dict]:
        return [{"id": "1"}, {"id": "2"}]

    def fetch(self, query: str, *params) -> list[dict]:
        return [{"result": True}]

    def execute(self, query: str, *params) -> str:
        return "OK"


def test_database_creation():
    db = Database()
    assert db is not None


def test_database_implements_interface():
    db = Database()
    assert isinstance(db, IDatabase)


def test_register_provider():
    db = Database()
    provider = MockProvider()
    db.register_provider("test", provider)
    assert db.get_provider("test") is provider


def test_default_provider():
    db = Database()
    provider = MockProvider()
    db.register_provider("test", provider, is_default=True)
    assert db.get_provider() is provider


def test_delegation_to_provider():
    db = Database()
    provider = MockProvider()
    db.register_provider("test", provider, is_default=True)

    result = db.get("users", "123")
    assert result == {"id": "123", "table": "users"}


def test_no_providers_error():
    db = Database()
    with pytest.raises(KeyError):
        db.get("users", "123")


def test_multiple_providers():
    db = Database()
    provider1 = MockProvider()
    provider2 = MockProvider()

    db.register_provider("p1", provider1)
    db.register_provider("p2", provider2, is_default=True)

    assert db.get_provider() is provider2
    assert db.get_provider("p1") is provider1


def test_insert():
    db = Database()
    provider = MockProvider()
    db.register_provider("test", provider, is_default=True)

    result = db.insert("users", {"name": "test"})
    assert result == "new-id"


def test_update():
    db = Database()
    provider = MockProvider()
    db.register_provider("test", provider, is_default=True)

    result = db.update("users", "123", {"name": "new"})
    assert result == {"id": "123", "name": "new"}


def test_delete():
    db = Database()
    provider = MockProvider()
    db.register_provider("test", provider, is_default=True)

    result = db.delete("users", "123")
    assert result is True


def test_count():
    db = Database()
    provider = MockProvider()
    db.register_provider("test", provider, is_default=True)

    result = db.count("users")
    assert result == 10


def test_list():
    db = Database()
    provider = MockProvider()
    db.register_provider("test", provider, is_default=True)

    result = db.list("users")
    assert len(result) == 2
