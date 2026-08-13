"""Unit-тесты для Task — Universal Task System."""
import pytest
from core.task import Task, TaskStatus, TaskType


class TestTaskCreation:
    """Создание задач."""

    def test_create_defaults(self):
        """Task.create() создаёт задачу с дефолтами."""
        task = Task.create(module_id="db", fn_name="get_user")
        assert task.module_id == "db"
        assert task.fn_name == "get_user"
        assert task.task_type == TaskType.UNKNOWN
        assert task.status == TaskStatus.PENDING
        assert task.payload == {}
        assert task.metadata == {}
        assert task.priority == 0
        assert task.id is not None

    def test_create_with_type(self):
        """Task.create() принимает task_type."""
        task = Task.create(
            module_id="api",
            fn_name="fetch_data",
            task_type=TaskType.NETWORK,
        )
        assert task.task_type == TaskType.NETWORK

    def test_create_with_payload(self):
        """Task.create() принимает payload."""
        task = Task.create(
            module_id="db",
            fn_name="insert_user",
            payload={"name": "Alice"},
        )
        assert task.payload == {"name": "Alice"}

    def test_create_with_priority(self):
        """Task.create() принимает priority."""
        task = Task.create(module_id="core", fn_name="compute", priority=5)
        assert task.priority == 5

    def test_uuid_unique(self):
        """Каждая задача имеет уникальный UUID."""
        t1 = Task.create(module_id="a", fn_name="f")
        t2 = Task.create(module_id="a", fn_name="f")
        assert t1.id != t2.id

    def test_created_at_set(self):
        """created_at устанавливается при создании."""
        task = Task.create(module_id="x", fn_name="y")
        assert task.created_at > 0


class TestTaskLifecycle:
    """Жизненный цикл задачи."""

    def test_start(self):
        """start() переключает в RUNNING."""
        task = Task.create(module_id="db", fn_name="query")
        task.start()
        assert task.status == TaskStatus.RUNNING
        assert task.started_at is not None

    def test_complete(self):
        """complete() записывает результат и duration."""
        task = Task.create(module_id="db", fn_name="get")
        task.start()
        task.complete(result=42)
        assert task.status == TaskStatus.COMPLETED
        assert task.result == 42
        assert task.completed_at is not None
        assert task.duration is not None
        assert task.duration >= 0

    def test_fail(self):
        """fail() записывает ошибку и duration."""
        task = Task.create(module_id="db", fn_name="write")
        task.start()
        task.fail("connection refused")
        assert task.status == TaskStatus.FAILED
        assert task.error == "connection refused"
        assert task.duration is not None

    def test_timeout(self):
        """timeout() фиксирует превышение лимита."""
        task = Task.create(module_id="io", fn_name="read")
        task.start()
        task.timeout()
        assert task.status == TaskStatus.TIMEOUT
        assert task.duration is not None

    def test_complete_without_start(self):
        """complete() без start() — duration None."""
        task = Task.create(module_id="x", fn_name="y")
        task.complete(result="ok")
        assert task.duration is None

    def test_fail_without_start(self):
        """fail() без start() — duration None."""
        task = Task.create(module_id="x", fn_name="y")
        task.fail("err")
        assert task.duration is None


class TestTaskSerialization:
    """Сериализация."""

    def test_to_dict_fields(self):
        """to_dict() содержит все поля."""
        task = Task.create(
            module_id="db",
            fn_name="query",
            task_type=TaskType.DATABASE,
            payload={"sql": "SELECT 1"},
            priority=3,
        )
        d = task.to_dict()
        assert d["module_id"] == "db"
        assert d["fn_name"] == "query"
        assert d["task_type"] == "database"
        assert d["status"] == "pending"
        assert d["payload"] == {"sql": "SELECT 1"}
        assert d["priority"] == 3
        assert "id" in d

    def test_to_dict_uuid_string(self):
        """UUID сериализуется как строка."""
        task = Task.create(module_id="x", fn_name="y")
        d = task.to_dict()
        assert isinstance(d["id"], str)

    def test_to_dict_enum_values(self):
        """Enum-значения сериализуются как строки."""
        task = Task.create(
            module_id="x", fn_name="y", task_type=TaskType.CPU
        )
        d = task.to_dict()
        assert d["task_type"] == "cpu"
        assert d["status"] == "pending"

    def test_to_dict_after_lifecycle(self):
        """to_dict() после complete() содержит result и duration."""
        task = Task.create(module_id="x", fn_name="y")
        task.start()
        task.complete(result="done")
        d = task.to_dict()
        assert d["result"] == "done"
        assert d["duration"] is not None
        assert d["started_at"] is not None
        assert d["completed_at"] is not None


class TestTaskEnums:
    """Enum-ы."""

    def test_all_statuses(self):
        """TaskStatus содержит все статусы."""
        statuses = [s.value for s in TaskStatus]
        assert "pending" in statuses
        assert "classified" in statuses
        assert "dispatched" in statuses
        assert "running" in statuses
        assert "completed" in statuses
        assert "failed" in statuses
        assert "timeout" in statuses

    def test_all_types(self):
        """TaskType содержит все типы."""
        types = [t.value for t in TaskType]
        assert "io" in types
        assert "cpu" in types
        assert "gpu" in types
        assert "network" in types
        assert "database" in types
        assert "aggregate" in types
        assert "unknown" in types
