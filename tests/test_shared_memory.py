"""Unit-тесты для SharedMemory: IStorage, LocalStorage, TaskQueue, ResultStore, SharedMemory."""
from __future__ import annotations

import time
from uuid import uuid4

import pytest

from core.shared_memory import (
    IStorage,
    LocalStorage,
    RedisStorage,
    SharedMemoryPool,
    TaskData,
    TaskQueue,
    ResultStore,
    SharedMemory,
)


# === Фикстуры ===

@pytest.fixture
def pool():
    """Создаёт SharedMemoryPool и чистит после теста."""
    p = SharedMemoryPool(num_blocks=16, block_size=4096)
    yield p
    p.cleanup()


@pytest.fixture
def local_storage(pool):
    """Создаёт LocalStorage."""
    return LocalStorage(pool)


@pytest.fixture
def result_store(local_storage):
    """Создаёт ResultStore с local storage."""
    return ResultStore(local_storage, ttl=60.0)


@pytest.fixture
def shared_memory():
    """Создаёт SharedMemory (local) и чистит после теста."""
    sm = SharedMemory(backend="local", num_blocks=16, block_size=4096)
    sm.start()
    yield sm
    sm.shutdown()


# ============================================================
# IStorage / LocalStorage
# ============================================================

class TestLocalStorage:
    """LocalStorage — хранилище на основе shared memory."""

    def test_set_and_get(self, local_storage):
        """set() сохраняет данные, get() возвращает их."""
        local_storage.set("key1", b"hello")
        assert local_storage.get("key1") == b"hello"

    def test_get_missing(self, local_storage):
        """get() для несуществующего ключа → None."""
        assert local_storage.get("nonexistent") is None

    def test_delete(self, local_storage):
        """delete() удаляет данные."""
        local_storage.set("key1", b"data")
        local_storage.delete("key1")
        assert local_storage.get("key1") is None

    def test_exists(self, local_storage):
        """exists() проверяет наличие ключа."""
        assert local_storage.exists("key1") is False
        local_storage.set("key1", b"data")
        assert local_storage.exists("key1") is True

    def test_overwrite(self, local_storage):
        """Перезапись данных по существующему ключу."""
        local_storage.set("key1", b"old")
        local_storage.set("key1", b"new")
        assert local_storage.get("key1") == b"new"

    def test_multiple_keys(self, local_storage):
        """Несколько ключей не пересекаются."""
        local_storage.set("a", b"1")
        local_storage.set("b", b"2")
        assert local_storage.get("a") == b"1"
        assert local_storage.get("b") == b"2"

    def test_large_data(self, local_storage):
        """Запись данных, заполняющих почти весь блок."""
        data = b"x" * 4000
        local_storage.set("big", data)
        assert local_storage.get("big") == data

    def test_data_too_large(self, local_storage):
        """Данные больше размера блока → ValueError."""
        with pytest.raises(ValueError, match="too large"):
            local_storage.set("big", b"x" * 5000)

    def test_cleanup(self, local_storage):
        """cleanup() очищает все данные."""
        local_storage.set("a", b"1")
        local_storage.set("b", b"2")
        local_storage.cleanup()
        assert local_storage.get("a") is None
        assert local_storage.get("b") is None


# ============================================================
# TaskData
# ============================================================

class TestTaskData:
    """TaskData — сериализация задачи."""

    def test_serialize_deserialize(self):
        """serialize() → deserialize() возвращает оригинал."""
        task = TaskData(
            uuid="test-uuid-123",
            function_name="my_func",
            module_name="my_module",
            args_serialized=b"args",
            kwargs_serialized=b"kwargs",
            created_at=time.time(),
            priority=1,
        )
        data = task.serialize()
        restored = TaskData.deserialize(data)
        assert restored.uuid == task.uuid
        assert restored.function_name == task.function_name
        assert restored.module_name == task.module_name
        assert restored.args_serialized == task.args_serialized
        assert restored.kwargs_serialized == task.kwargs_serialized
        assert restored.priority == task.priority

    def test_serialize_with_complex_args(self):
        """Сериализация сложных аргументов (dict, list, nested)."""
        import pickle
        args = ([1, 2, 3], {"key": "value", "nested": [4, 5]})
        kwargs = {"debug": True, "config": {"timeout": 30}}
        task = TaskData(
            uuid="test-uuid",
            function_name="func",
            module_name="mod",
            args_serialized=pickle.dumps(args),
            kwargs_serialized=pickle.dumps(kwargs),
            created_at=time.time(),
        )
        restored = TaskData.deserialize(task.serialize())
        assert pickle.loads(restored.args_serialized) == args
        assert pickle.loads(restored.kwargs_serialized) == kwargs


# ============================================================
# TaskQueue
# ============================================================

class TestTaskQueue:
    """TaskQueue — очередь задач через IStorage."""

    def _make_task(self, uuid: str = "task-1") -> TaskData:
        return TaskData(
            uuid=uuid,
            function_name="fn",
            module_name="mod",
            args_serialized=b"",
            kwargs_serialized=b"",
            created_at=time.time(),
        )

    def test_enqueue_dequeue(self, local_storage):
        """enqueue() → dequeue() возвращает задачу."""
        q = TaskQueue(local_storage, capacity=8)
        task = self._make_task()
        assert q.enqueue(task) is True
        result = q.dequeue()
        assert result is not None
        assert result.uuid == task.uuid

    def test_fifo_order(self, local_storage):
        """Очередь работает в режиме FIFO."""
        q = TaskQueue(local_storage, capacity=8)
        for i in range(3):
            q.enqueue(self._make_task(f"task-{i}"))
        results = [q.dequeue().uuid for _ in range(3)]
        assert results == ["task-0", "task-1", "task-2"]

    def test_size(self, local_storage):
        """size() отражает количество задач."""
        q = TaskQueue(local_storage, capacity=8)
        assert q.size() == 0
        q.enqueue(self._make_task())
        assert q.size() == 1
        q.dequeue()
        assert q.size() == 0

    def test_dequeue_empty_timeout(self, local_storage):
        """dequeue() из пустой очереди с таймаутом → None."""
        q = TaskQueue(local_storage, capacity=8)
        result = q.dequeue(timeout=0.05)
        assert result is None

    def test_clear(self, local_storage):
        """clear() очищает очередь."""
        q = TaskQueue(local_storage, capacity=8)
        q.enqueue(self._make_task("a"))
        q.enqueue(self._make_task("b"))
        q.clear()
        assert q.size() == 0
        assert q.dequeue(timeout=0.05) is None

    def test_capacity_limit(self, local_storage):
        """При заполнении очереди enqueue() возвращает False."""
        q = TaskQueue(local_storage, capacity=2)
        q.enqueue(self._make_task("a"))
        q.enqueue(self._make_task("b"))
        assert q.enqueue(self._make_task("c"), timeout=0.05) is False


# ============================================================
# ResultStore
# ============================================================

class TestResultStore:
    """ResultStore — хранилище результатов задач."""

    def test_set_and_get(self, result_store):
        """set() сохраняет результат, get() возвращает его."""
        uid = uuid4()
        result_store.set(uid, "hello")
        assert result_store.get(uid) == "hello"

    def test_get_missing(self, result_store):
        """get() для несуществующего UUID → None."""
        uid = uuid4()
        assert result_store.get(uid) is None

    def test_delete(self, result_store):
        """delete() удаляет результат."""
        uid = uuid4()
        result_store.set(uid, 42)
        assert result_store.delete(uid) is True
        assert result_store.get(uid) is None

    def test_delete_missing(self, result_store):
        """delete() для несуществующего UUID → False."""
        uid = uuid4()
        assert result_store.delete(uid) is False

    def test_exists(self, result_store):
        """exists() проверяет наличие результата."""
        uid = uuid4()
        assert result_store.exists(uid) is False
        result_store.set(uid, "data")
        assert result_store.exists(uid) is True

    def test_clear(self, result_store):
        """clear() очищает все результаты."""
        result_store.set(uuid4(), "a")
        result_store.set(uuid4(), "b")
        result_store.clear()
        assert result_store.get(uuid4()) is None

    def test_complex_result(self, result_store):
        """Хранение сложных объектов (dict, list, nested)."""
        uid = uuid4()
        complex_data = {"key": [1, 2, 3], "nested": {"a": True}}
        result_store.set(uid, complex_data)
        assert result_store.get(uid) == complex_data

    def test_shutdown(self, result_store):
        """shutdown() очищает результаты."""
        result_store.set(uuid4(), "data")
        result_store.shutdown()
        assert result_store.get(uuid4()) is None


# ============================================================
# SharedMemory (фасад)
# ============================================================

class TestSharedMemory:
    """SharedMemory — фасад для работы с общей памятью."""

    def test_submit_and_get_task(self, shared_memory):
        """submit_task() → get_task() возвращает задачу."""
        task = TaskData(
            uuid="test-uuid",
            function_name="fn",
            module_name="mod",
            args_serialized=b"",
            kwargs_serialized=b"",
            created_at=time.time(),
        )
        assert shared_memory.submit_task(task) is True
        result = shared_memory.get_task()
        assert result is not None
        assert result.uuid == "test-uuid"

    def test_store_and_get_result(self, shared_memory):
        """store_result() → get_result() возвращает результат."""
        uid = uuid4()
        shared_memory.store_result(uid, "result_data")
        assert shared_memory.get_result(uid) == "result_data"

    def test_get_result_timeout(self, shared_memory):
        """get_result() с таймаутом для несуществующего результата → None."""
        uid = uuid4()
        result = shared_memory.get_result(uid, timeout=0.05)
        assert result is None

    def test_queue_size(self, shared_memory):
        """queue.size() отражает количество задач."""
        assert shared_memory.queue.size() == 0
        task = TaskData(
            uuid="t1", function_name="f", module_name="m",
            args_serialized=b"", kwargs_serialized=b"",
            created_at=time.time(),
        )
        shared_memory.submit_task(task)
        assert shared_memory.queue.size() == 1

    def test_shutdown(self, shared_memory):
        """shutdown() очищает все ресурсы."""
        uid = uuid4()
        shared_memory.store_result(uid, "data")
        shared_memory.shutdown()
        assert shared_memory.get_result(uid) is None


# ============================================================
# Thread safety
# ============================================================

class TestThreadSafety:
    """Параллельные операции не падают."""

    def test_concurrent_set_get(self, local_storage):
        """Параллельные set/get в LocalStorage."""
        import threading
        errors = []

        def writer(n):
            try:
                for i in range(50):
                    local_storage.set(f"key_{n}_{i}", b"data")
            except Exception as e:
                errors.append(e)

        def reader():
            try:
                for _ in range(50):
                    local_storage.get("key_0_0")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
        threads += [threading.Thread(target=reader) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert errors == []
