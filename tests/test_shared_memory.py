"""Unit-тесты для SharedMemoryManager (core.shared_memory)."""
from __future__ import annotations

import time
from uuid import uuid4

import pytest

from core.shared_memory import SharedMemoryManager


# === Фикстуры ===

@pytest.fixture
def manager():
    """Создаёт SharedMemoryManager и чистит после теста."""
    m = SharedMemoryManager(ttl=60.0)
    yield m
    m.shutdown()


# === Базовые тесты ===

def test_set_and_get(manager):
    """set() сохраняет результат, get() возвращает его."""
    uid = uuid4()
    manager.set(uid, "hello")
    assert manager.get(uid) == "hello"


def test_get_missing(manager):
    """get() для несуществующего UUID → None."""
    uid = uuid4()
    assert manager.get(uid) is None


def test_delete(manager):
    """delete() удаляет результат."""
    uid = uuid4()
    manager.set(uid, 42)
    assert manager.delete(uid) is True
    assert manager.get(uid) is None


def test_delete_missing(manager):
    """delete() для несуществующего UUID → False."""
    uid = uuid4()
    assert manager.delete(uid) is False


def test_exists(manager):
    """exists() проверяет наличие результата."""
    uid = uuid4()
    assert manager.exists(uid) is False
    manager.set(uid, "data")
    assert manager.exists(uid) is True


def test_clear(manager):
    """clear() очищает все результаты."""
    manager.set(uuid4(), "a")
    manager.set(uuid4(), "b")
    assert manager.size == 2
    manager.clear()
    assert manager.size == 0


def test_size(manager):
    """size() возвращает количество хранимых результатов."""
    assert manager.size == 0
    manager.set(uuid4(), "a")
    assert manager.size == 1
    manager.set(uuid4(), "b")
    assert manager.size == 2


# === TTL и очистка ===

def test_ttl_eviction(manager):
    """Результаты с TTL удаляются после истечения времени."""
    short_ttl_manager = SharedMemoryManager(ttl=0.1)
    try:
        uid = uuid4()
        short_ttl_manager.set(uid, "expire_me")
        assert short_ttl_manager.get(uid) == "expire_me"
        time.sleep(0.2)
        short_ttl_manager._cleanup_expired()
        assert short_ttl_manager.get(uid) is None
    finally:
        short_ttl_manager.shutdown()


def test_no_ttl_if_zero():
    """TTL=0 → результаты не удаляются автоматически."""
    m = SharedMemoryManager(ttl=0)
    try:
        uid = uuid4()
        m.set(uid, "forever")
        time.sleep(0.1)
        m._cleanup_expired()
        assert m.get(uid) == "forever"
    finally:
        m.shutdown()


# === Ring buffer (eviction) ===

def test_evict_oldest_on_max():
    """При превышении max_results удаляется самый старый результат."""
    m = SharedMemoryManager(max_results=3, ttl=0)
    try:
        ids = [uuid4() for _ in range(4)]
        for i, uid in enumerate(ids):
            m.set(uid, f"result_{i}")

        # Самый старый (ids[0]) должен быть удалён
        assert m.size == 3
        assert m.get(ids[0]) is None
        assert m.get(ids[1]) == "result_1"
        assert m.get(ids[3]) == "result_3"
    finally:
        m.shutdown()


# === Thread safety ===

def test_concurrent_set_get():
    """Параллельные set/get не падают."""
    import threading

    m = SharedMemoryManager(ttl=0)
    try:
        errors = []

        def writer(n):
            try:
                for i in range(100):
                    uid = uuid4()
                    m.set(uid, f"val_{n}_{i}")
            except Exception as e:
                errors.append(e)

        def reader():
            try:
                for _ in range(100):
                    m.size
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
        threads += [threading.Thread(target=reader) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert errors == []
    finally:
        m.shutdown()


# === Shutdown ===

def test_shutdown(manager):
    """shutdown() останавливает фоновый поток и очищает результаты."""
    manager.set(uuid4(), "data")
    manager.shutdown()
    assert manager.size == 0
    assert manager._running is False
