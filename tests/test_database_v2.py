"""Тесты Database v2 — интеграция с Universal Task System."""
from __future__ import annotations

import pytest
from typing import Any
from unittest.mock import MagicMock

from core.database import Database
from core.task import Task, TaskStatus, TaskType
from core.task_store import TaskStore
from core.stats_batch_writer import StatsBatchWriter


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
def task_store() -> TaskStore:
    return TaskStore()


@pytest.fixture
def db_with_tasks(provider: InMemoryProvider, task_store: TaskStore) -> Database:
    """Database с подключённым Task System (без реального stats_writer — мок)."""
    stats_writer = MagicMock(spec=StatsBatchWriter)
    db = Database(task_store=task_store, stats_writer=stats_writer)
    db.register_provider("mem", provider, is_default=True)
    return db


@pytest.fixture
def db_no_tasks(provider: InMemoryProvider) -> Database:
    """Database без Task System — обратная совместимость."""
    db = Database(cache=None, dispatcher=None)
    db.register_provider("mem", provider, is_default=True)
    return db


# ── 1. Task создаётся при каждой CRUD-операции ─────────


class TestTaskCreation:
    """Task создаётся и добавляется в TaskStore при каждой CRUD-операции."""

    def test_get_creates_task(self, db_with_tasks: Database, task_store: TaskStore, provider: InMemoryProvider) -> None:
        provider._store["users:1"] = {"id": "1", "name": "Alice"}

        db_with_tasks.get("users", "1")

        history = task_store.get_history()
        assert len(history) == 1
        task = history[0]
        assert task.fn_name == "get"
        assert task.module_id == "database"
        assert task.task_type == TaskType.DATABASE
        assert task.status == TaskStatus.COMPLETED
        assert task.result == {"id": "1", "name": "Alice"}

    def test_get_by_field_creates_task(
        self, db_with_tasks: Database, task_store: TaskStore, provider: InMemoryProvider
    ) -> None:
        provider._store["users:1"] = {"id": "1", "email": "a@b.com"}

        db_with_tasks.get_by_field("users", "email", "a@b.com")

        history = task_store.get_history()
        assert len(history) == 1
        task = history[0]
        assert task.fn_name == "get_by_field"
        assert task.status == TaskStatus.COMPLETED

    def test_insert_creates_task(self, db_with_tasks: Database, task_store: TaskStore) -> None:
        result = db_with_tasks.insert("users", {"name": "Bob"})

        history = task_store.get_history()
        assert len(history) == 1
        task = history[0]
        assert task.fn_name == "insert"
        assert task.status == TaskStatus.COMPLETED
        assert task.result == result

    def test_update_creates_task(
        self, db_with_tasks: Database, task_store: TaskStore, provider: InMemoryProvider
    ) -> None:
        provider._store["users:1"] = {"id": "1", "name": "Alice"}

        db_with_tasks.update("users", "1", {"name": "Bob"})

        history = task_store.get_history()
        assert len(history) == 1
        task = history[0]
        assert task.fn_name == "update"
        assert task.status == TaskStatus.COMPLETED

    def test_delete_creates_task(
        self, db_with_tasks: Database, task_store: TaskStore, provider: InMemoryProvider
    ) -> None:
        provider._store["users:1"] = {"id": "1"}

        db_with_tasks.delete("users", "1")

        history = task_store.get_history()
        assert len(history) == 1
        task = history[0]
        assert task.fn_name == "delete"
        assert task.status == TaskStatus.COMPLETED
        assert task.result is True

    def test_multiple_operations_create_multiple_tasks(
        self, db_with_tasks: Database, task_store: TaskStore, provider: InMemoryProvider
    ) -> None:
        id1 = db_with_tasks.insert("users", {"name": "Alice"})
        db_with_tasks.get("users", id1)
        db_with_tasks.update("users", id1, {"name": "Bob"})
        db_with_tasks.delete("users", id1)

        history = task_store.get_history()
        assert len(history) == 4
        # get_history() возвращает newest-first
        fn_names = [t.fn_name for t in history]
        assert fn_names == ["delete", "update", "get", "insert"]


# ── 2. TaskStore получает задачу ───────────────────────


class TestTaskStoreIntegration:
    """TaskStore получает и трекает задачи."""

    def test_task_in_store_after_get(
        self, db_with_tasks: Database, task_store: TaskStore, provider: InMemoryProvider
    ) -> None:
        provider._store["t:1"] = {"id": "1"}
        db_with_tasks.get("t", "1")

        stats = task_store.stats()
        assert stats["completed"] == 1
        assert stats["active"] == 0

    def test_task_has_correct_timing(
        self, db_with_tasks: Database, task_store: TaskStore, provider: InMemoryProvider
    ) -> None:
        provider._store["t:1"] = {"id": "1"}
        db_with_tasks.get("t", "1")

        task = task_store.get_history()[0]
        assert task.created_at is not None
        assert task.started_at is not None
        assert task.completed_at is not None
        assert task.duration is not None
        assert task.duration >= 0

    def test_task_recorded_on_error(self, db_with_tasks: Database, task_store: TaskStore) -> None:
        """При ошибке — task в истории со статусом FAILED."""
        # Провайдер без метода get вызовет AttributeError
        class BrokenProvider:
            def insert(self, table, data):
                return "1"
            def get(self, table, id):
                raise RuntimeError("DB down")

        db_with_tasks.register_provider("broken", BrokenProvider(), is_default=True)

        with pytest.raises(RuntimeError, match="DB down"):
            db_with_tasks.get("t", "1")

        history = task_store.get_history()
        assert len(history) == 1
        task = history[0]
        assert task.status == TaskStatus.FAILED
        assert "DB down" in task.error


# ── 3. StatsBatchWriter получает задачу ────────────────


class TestStatsWriterIntegration:
    """StatsBatchWriter.add() вызывается после каждой операции."""

    def test_stats_writer_receives_task_on_get(
        self, db_with_tasks: Database, task_store: TaskStore, provider: InMemoryProvider
    ) -> None:
        provider._store["t:1"] = {"id": "1"}
        stats_writer = db_with_tasks._stats_writer

        db_with_tasks.get("t", "1")

        stats_writer.add.assert_called_once()
        task_arg = stats_writer.add.call_args[0][0]
        assert isinstance(task_arg, Task)
        assert task_arg.fn_name == "get"

    def test_stats_writer_receives_task_on_insert(
        self, db_with_tasks: Database, task_store: TaskStore
    ) -> None:
        stats_writer = db_with_tasks._stats_writer

        db_with_tasks.insert("t", {"x": 1})

        stats_writer.add.assert_called_once()
        task_arg = stats_writer.add.call_args[0][0]
        assert task_arg.fn_name == "insert"

    def test_stats_writer_receives_task_on_error(
        self, db_with_tasks: Database, task_store: TaskStore
    ) -> None:
        class FailProvider:
            def get(self, table, id):
                raise ValueError("fail")

        db_with_tasks.register_provider("fail", FailProvider(), is_default=True)
        stats_writer = db_with_tasks._stats_writer

        with pytest.raises(ValueError):
            db_with_tasks.get("t", "1")

        stats_writer.add.assert_called_once()
        task_arg = stats_writer.add.call_args[0][0]
        assert task_arg.status == TaskStatus.FAILED


# ── 4. Обратная совместимость ──────────────────────────


class TestBackwardCompatibility:
    """Database без Task System работает как раньше."""

    def test_get_without_task_system(self, db_no_tasks: Database, provider: InMemoryProvider) -> None:
        provider._store["users:1"] = {"id": "1", "name": "Alice"}
        result = db_no_tasks.get("users", "1")
        assert result == {"id": "1", "name": "Alice"}

    def test_insert_without_task_system(self, db_no_tasks: Database) -> None:
        result = db_no_tasks.insert("users", {"name": "Bob"})
        assert result == "1"

    def test_no_task_store_no_errors(self, db_no_tasks: Database) -> None:
        """Если task_store=None — задачи не создаются, ошибок нет."""
        assert db_no_tasks._task_store is None
        assert db_no_tasks._stats_writer is None

        # CRUD работает без задач
        id_ = db_no_tasks.insert("t", {"v": 1})
        assert db_no_tasks.get("t", id_) == {"id": "1", "v": 1}
        assert db_no_tasks.update("t", id_, {"v": 2}) == {"id": "1", "v": 2}
        assert db_no_tasks.delete("t", id_) is True


# ── 5. Factory тест ────────────────────────────────────


class TestFactory:
    """DatabaseFactory.create_with_task_system корректно создаёт все компоненты."""

    def test_create_with_task_system(self) -> None:
        from core.factories import DatabaseFactory

        database, task_store, stats_writer = DatabaseFactory.create_with_task_system()

        assert isinstance(database, Database)
        assert isinstance(task_store, TaskStore)
        assert isinstance(stats_writer, StatsBatchWriter)
        assert database._task_store is task_store
        assert database._stats_writer is stats_writer
        assert stats_writer._db is database

    def test_create_without_task_system(self) -> None:
        from core.factories import DatabaseFactory

        database = DatabaseFactory.create()

        assert isinstance(database, Database)
        assert database._task_store is None
        assert database._stats_writer is None
