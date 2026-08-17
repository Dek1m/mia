"""SharedMemory — модуль общей памяти: IStorage, LocalStorage, RedisStorage, TaskData, TaskQueue, ResultStore, SharedMemory."""
from __future__ import annotations

import pickle
import struct
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from argenta_logging import get_logger

log = get_logger(__name__)

__all__ = [
    "IStorage",
    "LocalStorage",
    "RedisStorage",
    "TaskData",
    "TaskQueue",
    "ResultStore",
    "SharedMemory",
    "SharedMemoryPool",
]


# ---------------------------------------------------------------------------
# IStorage — интерфейс хранилища
# ---------------------------------------------------------------------------

class IStorage(ABC):
    """Абстрактный интерфейс хранилища ключ-значение."""

    @abstractmethod
    def get(self, key: str) -> bytes | None:
        """Получить значение по ключу."""
        ...

    @abstractmethod
    def set(self, key: str, value: bytes, ttl: float = 0) -> None:
        """Сохранить значение по ключу. ttl=0 — бессрочно."""
        ...

    @abstractmethod
    def delete(self, key: str) -> None:
        """Удалить ключ."""
        ...

    @abstractmethod
    def exists(self, key: str) -> bool:
        """Проверить наличие ключа."""
        ...

    @abstractmethod
    def cleanup(self) -> None:
        """Очистить все данные."""
        ...


# ---------------------------------------------------------------------------
# LocalStorage — хранилище в общей памяти (multiprocessing.shared_memory)
# ---------------------------------------------------------------------------

import multiprocessing.shared_memory as _shm


class SharedMemoryPool:
    """Пул именованных блоков shared memory с ленивым выделением."""

    def __init__(self, num_blocks: int = 64, block_size: int = 1_048_576) -> None:
        from uuid import uuid4 as _uuid4
        self._num_blocks = num_blocks
        self._block_size = block_size
        self._prefix = f"mia_{_uuid4().hex[:8]}_"
        self._blocks: dict[str, _shm.SharedMemory] = {}
        self._lock = threading.Lock()

    def _full_name(self, block_name: str) -> str:
        """Полное имя блока с уникальным префиксом."""
        return f"{self._prefix}{block_name}"

    def allocate(self, block_name: str) -> _shm.SharedMemory:
        """Выделить блок по имени, создать если не существует."""
        full_name = self._full_name(block_name)
        with self._lock:
            if block_name in self._blocks:
                return self._blocks[block_name]
            try:
                block = _shm.SharedMemory(name=full_name, create=False)
            except FileNotFoundError:
                block = _shm.SharedMemory(name=full_name, create=True, size=self._block_size)
            self._blocks[block_name] = block
            return block

    def attach(self, block_name: str) -> _shm.SharedMemory | None:
        """Подключиться к существующему блоку. None если не найден."""
        full_name = self._full_name(block_name)
        with self._lock:
            if block_name in self._blocks:
                return self._blocks[block_name]
            try:
                block = _shm.SharedMemory(name=full_name, create=False)
                self._blocks[block_name] = block
                return block
            except FileNotFoundError:
                return None

    def free(self, block_name: str) -> None:
        """Отсоединить блок (close)."""
        with self._lock:
            block = self._blocks.pop(block_name, None)
            if block:
                # Обнуляем буфер перед закрытием, чтобы данные не висели
                block.buf[:4] = b'\x00\x00\x00\x00'
                block.close()

    def cleanup(self) -> None:
        """Закрыть и удалить все блоки."""
        with self._lock:
            for block in self._blocks.values():
                try:
                    block.close()
                    block.unlink()
                except Exception:
                    pass
            self._blocks.clear()

    @property
    def block_size(self) -> int:
        """Размер блока в байтах."""
        return self._block_size


class LocalStorage(IStorage):
    """Хранилище на основе multiprocessing.shared_memory.

    Thread-safe. Каждый ключ — отдельный блок shared memory.
    """

    def __init__(self, pool: SharedMemoryPool | None = None) -> None:
        self._pool = pool or SharedMemoryPool()
        self._lock = threading.Lock()

    def get(self, key: str) -> bytes | None:
        """Прочитать данные из блока по ключу."""
        block = self._pool.attach(key)
        if block is None:
            return None
        try:
            # Первые 4 байта — длина данных (u32)
            length = struct.unpack("I", block.buf[:4])[0]
            if length == 0:
                return None
            return bytes(block.buf[4:4 + length])
        except Exception:
            return None

    def set(self, key: str, value: bytes, ttl: float = 0) -> None:
        """Записать данные в блок. ttl игнорируется для local storage."""
        block = self._pool.allocate(key)
        with self._lock:
            if len(value) + 4 > len(block.buf):
                raise ValueError(
                    f"Data too large: {len(value) + 4} bytes > {len(block.buf)} bytes"
                )
            # Первые 4 байта — длина данных
            block.buf[:4] = struct.pack("I", len(value))
            block.buf[4:4 + len(value)] = value

    def delete(self, key: str) -> None:
        """Удалить блок."""
        self._pool.free(key)

    def exists(self, key: str) -> bool:
        """Проверить наличие блока."""
        return self._pool.attach(key) is not None

    def cleanup(self) -> None:
        """Очистить все блоки."""
        self._pool.cleanup()


# ---------------------------------------------------------------------------
# RedisStorage — хранилище в Redis (опциональная зависимость)
# ---------------------------------------------------------------------------

class RedisStorage(IStorage):
    """Хранилище на основе Redis.

    Ключи: mia:queue:pending, mia:result:{uuid}, mia:lock:{key}
    """

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379",
        prefix: str = "mia:",
        default_ttl: float = 300.0,
    ) -> None:
        try:
            import redis
            self._redis = redis.Redis.from_url(redis_url, decode_responses=False)
            self._redis.ping()
        except ImportError:
            raise ImportError(
                "RedisStorage requires 'redis' package: pip install redis"
            )
        except Exception as e:
            raise ConnectionError(f"Cannot connect to Redis: {e}")
        self._prefix = prefix
        self._default_ttl = default_ttl

    def _key(self, key: str) -> str:
        """Добавить префикс к ключу."""
        return f"{self._prefix}{key}"

    def get(self, key: str) -> bytes | None:
        """Получить значение из Redis."""
        return self._redis.get(self._key(key))

    def set(self, key: str, value: bytes, ttl: float = 0) -> None:
        """Сохранить значение в Redis с опциональным TTL."""
        effective_ttl = ttl if ttl > 0 else self._default_ttl
        self._redis.setex(self._key(key), effective_ttl, value)

    def delete(self, key: str) -> None:
        """Удалить ключ из Redis."""
        self._redis.delete(self._key(key))

    def exists(self, key: str) -> bool:
        """Проверить наличие ключа в Redis."""
        return bool(self._redis.exists(self._key(key)))

    def cleanup(self : "RedisStorage") -> None:
        """Удалить все ключи с префиксом."""
        keys = self._redis.keys(f"{self._prefix}*")
        if keys:
            self._redis.delete(*keys)


# ---------------------------------------------------------------------------
# TaskData — dataclass для сериализации задачи
# ---------------------------------------------------------------------------

@dataclass
class TaskData:
    """Сериализуемая задача для передачи через shared memory."""

    uuid: str
    function_name: str
    module_name: str
    args_serialized: bytes
    kwargs_serialized: bytes
    created_at: float
    priority: int = 0

    def serialize(self) -> bytes:
        """Сериализовать в bytes (pickle)."""
        return pickle.dumps(self)

    @classmethod
    def deserialize(cls, data: bytes) -> TaskData:
        """Десериализовать из bytes (pickle)."""
        return pickle.loads(data)


# ---------------------------------------------------------------------------
# TaskQueue — очередь задач через IStorage
# ---------------------------------------------------------------------------

class TaskQueue:
    """Очередь задач на основе IStorage.

    Кольцевой буфер в shared memory или список в Redis.
    """

    _METADATA_SIZE = 12  # head(u32) + tail(u32) + count(u32)

    def __init__(
        self,
        storage: IStorage,
        queue_name: str = "task_queue",
        capacity: int = 1024,
    ) -> None:
        self._storage = storage
        self._queue_name = queue_name
        self._capacity = capacity
        self._lock = threading.Lock()

    def _meta_key(self) -> str:
        """Ключ метаданных очереди."""
        return f"{self._queue_name}:meta"

    def _slot_key(self, index: int) -> str:
        """Ключ слота данных."""
        return f"{self._queue_name}:slot:{index}"

    def _read_metadata(self) -> tuple[int, int, int]:
        """Прочитать head, tail, count."""
        data = self._storage.get(self._meta_key())
        if data is None:
            return (0, 0, 0)
        return struct.unpack("III", data[:self._METADATA_SIZE])

    def _write_metadata(self, head: int, tail: int, count: int) -> None:
        """Записать head, tail, count."""
        data = struct.pack("III", head, tail, count)
        self._storage.set(self._meta_key(), data)

    def enqueue(self, task_data: TaskData, timeout: float = 1.0) -> bool:
        """Поставить задачу в очередь. True если успешно."""
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            with self._lock:
                head, tail, count = self._read_metadata()
                if count < self._capacity:
                    serialized = task_data.serialize()
                    self._storage.set(self._slot_key(tail), serialized)
                    new_tail = (tail + 1) % self._capacity
                    self._write_metadata(head, new_tail, count + 1)
                    return True
            time.sleep(0.001)
        return False

    def dequeue(self, timeout: float = 1.0) -> TaskData | None:
        """Взять задачу из очереди. None если таймаут."""
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            with self._lock:
                head, tail, count = self._read_metadata()
                if count > 0:
                    data = self._storage.get(self._slot_key(head))
                    if data is None:
                        # Повреждённый слот — пропускаем
                        new_head = (head + 1) % self._capacity
                        self._write_metadata(new_head, tail, count - 1)
                        continue
                    task_data = TaskData.deserialize(data)
                    new_head = (head + 1) % self._capacity
                    self._write_metadata(new_head, tail, count - 1)
                    return task_data
            time.sleep(0.001)
        return None

    def size(self) -> int:
        """Текущий размер очереди."""
        _, _, count = self._read_metadata()
        return count

    def clear(self) -> None:
        """Очистить очередь."""
        self._write_metadata(0, 0, 0)

    def cleanup(self) -> None:
        """Очистить все данные очереди из storage."""
        self.clear()


# ---------------------------------------------------------------------------
# ResultStore — хранилище результатов задач по UUID
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _ResultEntry:
    """Запись результата в хранилище."""

    result: Any
    created_at: float = field(default_factory=time.monotonic)


class ResultStore:
    """Потокобезопасное хранилище результатов задач по UUID.

    Используется для передачи результатов от воркеров к вызывающему коду.
    Каждый результат хранится с TTL для автоматической очистки.
    """

    def __init__(
        self,
        storage: IStorage,
        max_results: int = 25000,
        ttl: float = 300.0,
    ) -> None:
        self._storage = storage
        self._max_results = max_results
        self._ttl = ttl
        self._lock = threading.RLock()

    def _result_key(self, task_id: UUID) -> str:
        """Ключ результата в storage."""
        return f"result:{task_id}"

    def set(self, task_id: UUID, result: Any) -> None:
        """Сохранить результат задачи."""
        entry = _ResultEntry(result=result)
        data = pickle.dumps(entry)
        self._storage.set(self._result_key(task_id), data, ttl=self._ttl)

    def get(self, task_id: UUID) -> Any | None:
        """Получить результат задачи."""
        data = self._storage.get(self._result_key(task_id))
        if data is None:
            return None
        try:
            entry: _ResultEntry = pickle.loads(data)
            return entry.result
        except Exception:
            return None

    def delete(self, task_id: UUID) -> bool:
        """Удалить результат задачи."""
        key = self._result_key(task_id)
        if self._storage.exists(key):
            self._storage.delete(key)
            return True
        return False

    def exists(self, task_id: UUID) -> bool:
        """Проверить наличие результата."""
        return self._storage.exists(self._result_key(task_id))

    def clear(self) -> None:
        """Очистить все результаты (пересоздать storage)."""
        self._storage.cleanup()

    def shutdown(self) -> None:
        """Остановить хранилище."""
        self._storage.cleanup()


# ---------------------------------------------------------------------------
# SharedMemory — фасад (единая точка входа)
# ---------------------------------------------------------------------------

class SharedMemory:
    """Фасад для работы с общей памятью.

    Инкапсулирует хранилище, очередь задач и хранилище результатов.
    Поддерживает local (shared_memory) и redis бэкенды.
    """

    def __init__(
        self,
        backend: str = "local",
        redis_url: str = "redis://localhost:6379",
        redis_prefix: str = "mia:",
        result_ttl: float = 300.0,
        num_blocks: int = 64,
        block_size: int = 1_048_576,
        queue_capacity: int = 1024,
    ) -> None:
        self._backend = backend
        self._redis_url = redis_url
        self._redis_prefix = redis_prefix
        self._result_ttl = result_ttl
        self._num_blocks = num_blocks
        self._block_size = block_size
        self._queue_capacity = queue_capacity

        self._storage: IStorage | None = None
        self._queue: TaskQueue | None = None
        self._results: ResultStore | None = None

    def start(self) -> None:
        """Инициализировать storage, очередь и хранилище результатов."""
        if self._backend == "redis":
            self._storage = RedisStorage(
                redis_url=self._redis_url,
                prefix=self._redis_prefix,
                default_ttl=self._result_ttl,
            )
        else:
            pool = SharedMemoryPool(self._num_blocks, self._block_size)
            self._storage = LocalStorage(pool)

        self._queue = TaskQueue(self._storage, capacity=self._queue_capacity)
        self._results = ResultStore(self._storage, ttl=self._result_ttl)

    def shutdown(self) -> None:
        """Остановить всё, освободить ресурсы."""
        if self._results:
            self._results.shutdown()
        if self._storage:
            self._storage.cleanup()

    def submit_task(self, task_data: TaskData) -> bool:
        """Отправить задачу в очередь."""
        return self._queue.enqueue(task_data)

    def get_task(self) -> TaskData | None:
        """Забрать задачу из очереди."""
        return self._queue.dequeue()

    def store_result(self, task_id: UUID, result: Any) -> None:
        """Сохранить результат."""
        self._results.set(task_id, result)

    def get_result(self, task_id: UUID, timeout: float = 10.0) -> Any | None:
        """Получить результат с таймаутом ожидания."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self._results.get(task_id)
            if result is not None:
                return result
            time.sleep(0.001)
        return None

    @property
    def storage(self) -> IStorage:
        """Хранилище."""
        return self._storage

    @property
    def queue(self) -> TaskQueue:
        """Очередь задач."""
        return self._queue

    @property
    def results(self) -> ResultStore:
        """Хранилище результатов."""
        return self._results
