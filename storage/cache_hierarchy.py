"""Cache Hierarchy — многоуровневый кеш (L0 → L1 → L2).

L0: dict с TTL (in-process, fastest)
L1: SharedMemory (inter-process, optional)
L2: Redis (distributed, optional)

При get() данные ищутся L0 → L1 → L2.
При set() данные записываются во все активные уровни.
При delete() данные удаляются со всех уровней.
"""
from __future__ import annotations

import time
import threading
from typing import Any

from argenta_logging import get_logger
from core.interfaces import ICache
from monitoring.metrics import database_cache_hits_total, database_cache_misses_total

log = get_logger(__name__)


class CacheHierarchy(ICache):
    """Многоуровневый кеш.

    Args:
        l1_shm: SharedMemoryManager для L1 (None = L1 отключён)
        l1_segment: имя сегмента SharedMemory для L1
        l1_size: размер сегмента SharedMemory в байтах
        l2_redis: redis.Redis клиент для L2 (None = L2 отключён)
        default_ttl: TTL по умолчанию в секундах
    """

    def __init__(
        self,
        l1_shm: Any | None = None,
        l1_segment: str = "cache_l1",
        l1_size: int = 4 * 1024 * 1024,
        l2_redis: Any | None = None,
        default_ttl: int = 300,
    ) -> None:
        self._default_ttl = default_ttl

        # L0: in-process dict с TTL
        self._l0: dict[str, tuple[Any, float]] = {}
        self._l0_lock = threading.Lock()

        # L1: SharedMemory (optional)
        self._l1: Any | None = None
        self._l1_manager: Any = l1_shm
        self._l1_segment_name = l1_segment
        self._l1_size = l1_size
        self._l1_lock = threading.Lock()

        # L2: Redis (optional)
        self._l2: Any | None = l2_redis

        # Stats
        self._hits = 0
        self._misses = 0
        self._stats_lock = threading.Lock()

    @property
    def hits(self) -> int:
        return self._hits

    @property
    def misses(self) -> int:
        return self._misses

    @property
    def hit_rate(self) -> float:
        total = self._hits + self._misses
        return self._hits / total if total else 0.0

    def init_l1(self) -> None:
        """Инициализировать L1 SharedMemory (вызывать явно)."""
        if self._l1_manager is None:
            log.warning("Cannot init L1: no SharedMemoryManager provided")
            return
        try:
            self._l1 = self._l1_manager.create(self._l1_segment_name, self._l1_size)
            log.info("L1 cache initialized", extra={"segment": self._l1_segment_name, "size": self._l1_size})
        except Exception as e:
            log.error("Failed to init L1 cache", extra={"error": str(e)})
            self._l1 = None

    def get(self, key: str) -> Any | None:
        """Получить значение из кеша (L0 → L1 → L2)."""
        # L0
        with self._l0_lock:
            entry = self._l0.get(key)
            if entry is not None:
                value, expires = entry
                if expires > time.monotonic():
                    self._record_hit()
                    database_cache_hits_total.labels(level="l0").inc()
                    log.debug("cache_hit", extra={"key": key, "level": "l0"})
                    return value
                del self._l0[key]

        # L1
        if self._l1 is not None:
            value = self._get_l1(key)
            if value is not None:
                # Промотируем в L0
                self._set_l0(key, value)
                self._record_hit()
                database_cache_hits_total.labels(level="l1").inc()
                log.debug("cache_hit", extra={"key": key, "level": "l1"})
                return value

        # L2
        if self._l2 is not None:
            value = self._get_l2(key)
            if value is not None:
                # Промотируем в L0 и L1
                self._set_l0(key, value)
                self._set_l1(key, value)
                self._record_hit()
                database_cache_hits_total.labels(level="l2").inc()
                log.debug("cache_hit", extra={"key": key, "level": "l2"})
                return value

        self._record_miss()
        database_cache_misses_total.inc()
        log.debug("cache_miss", extra={"key": key})
        return None

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        """Записать значение во все активные уровни."""
        effective_ttl = ttl if ttl is not None else self._default_ttl
        self._set_l0(key, value, effective_ttl)
        self._set_l1(key, value)
        self._set_l2(key, value, effective_ttl)

    def delete(self, key: str) -> bool:
        """Удалить значение со всех уровней."""
        removed = False

        with self._l0_lock:
            if key in self._l0:
                del self._l0[key]
                removed = True

        if self._l1 is not None:
            removed = self._delete_l1(key) or removed

        if self._l2 is not None:
            removed = self._delete_l2(key) or removed

        return removed

    def exists(self, key: str) -> bool:
        """Проверить наличие ключа на любом уровне."""
        with self._l0_lock:
            entry = self._l0.get(key)
            if entry is not None:
                _, expires = entry
                if expires > time.monotonic():
                    return True
                del self._l0[key]

        if self._l1 is not None and self._exists_l1(key):
            return True

        if self._l2 is not None and self._exists_l2(key):
            return True

        return False

    def clear(self) -> None:
        """Очистить все уровни кеша."""
        with self._l0_lock:
            self._l0.clear()

        if self._l1 is not None:
            self._clear_l1()

        if self._l2 is not None:
            self._clear_l2()

    def stats(self) -> dict[str, Any]:
        """Статистика кеша."""
        with self._stats_lock:
            return {
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": self.hit_rate,
                "l0_size": len(self._l0),
                "l1_active": self._l1 is not None,
                "l2_active": self._l2 is not None,
            }

    # ── L0 (dict) ──────────────────────────────────

    def _set_l0(self, key: str, value: Any, ttl: int | None = None) -> None:
        effective_ttl = ttl if ttl is not None else self._default_ttl
        expires = time.monotonic() + effective_ttl
        with self._l0_lock:
            self._l0[key] = (value, expires)

    # ── L1 (SharedMemory) ─────────────────────────

    def _get_l1(self, key: str) -> Any | None:
        if self._l1 is None:
            return None
        try:
            from storage.serializer import Serializer
            # SharedMemory layout: [4 bytes length][pickled data]
            raw_length = int.from_bytes(self._l1.buf[:4], "little")
            if raw_length == 0:
                return None
            raw_data = bytes(self._l1.buf[4:4 + raw_length])
            data = Serializer.deserialize(raw_data)
            if isinstance(data, dict):
                return data.get(key)
        except Exception as e:
            log.debug("L1 get failed", extra={"key": key, "error": str(e)})
        return None

    def _set_l1(self, key: str, value: Any) -> None:
        if self._l1 is None:
            return
        try:
            from storage.serializer import Serializer
            # Read existing data, update, write back
            existing: dict[str, Any] = {}
            raw_length = int.from_bytes(self._l1.buf[:4], "little")
            if raw_length > 0:
                raw_data = bytes(self._l1.buf[4:4 + raw_length])
                data = Serializer.deserialize(raw_data)
                if isinstance(data, dict):
                    existing = data
            existing[key] = value
            serialized = Serializer.serialize(existing)
            if len(serialized) + 4 <= self._l1_size:
                with self._l1_lock:
                    self._l1.buf[:4] = len(serialized).to_bytes(4, "little")
                    self._l1.buf[4:4 + len(serialized)] = serialized
            else:
                log.warning("L1 segment full, skipping", extra={"key": key})
        except Exception as e:
            log.debug("L1 set failed", extra={"key": key, "error": str(e)})

    def _delete_l1(self, key: str) -> bool:
        if self._l1 is None:
            return False
        try:
            from storage.serializer import Serializer
            raw_length = int.from_bytes(self._l1.buf[:4], "little")
            if raw_length == 0:
                return False
            raw_data = bytes(self._l1.buf[4:4 + raw_length])
            data = Serializer.deserialize(raw_data)
            if isinstance(data, dict) and key in data:
                del data[key]
                serialized = Serializer.serialize(data)
                with self._l1_lock:
                    self._l1.buf[:4] = len(serialized).to_bytes(4, "little")
                    self._l1.buf[4:4 + len(serialized)] = serialized
                return True
        except Exception as e:
            log.debug("L1 delete failed", extra={"key": key, "error": str(e)})
        return False

    def _exists_l1(self, key: str) -> bool:
        if self._l1 is None:
            return False
        try:
            from storage.serializer import Serializer
            raw_length = int.from_bytes(self._l1.buf[:4], "little")
            if raw_length == 0:
                return False
            raw_data = bytes(self._l1.buf[4:4 + raw_length])
            data = Serializer.deserialize(raw_data)
            return isinstance(data, dict) and key in data
        except Exception:
            return False

    def _clear_l1(self) -> None:
        if self._l1 is None:
            return
        try:
            with self._l1_lock:
                self._l1.buf[:4] = b"\x00\x00\x00\x00"
        except Exception as e:
            log.debug("L1 clear failed", extra={"error": str(e)})

    # ── L2 (Redis) ─────────────────────────────────

    def _get_l2(self, key: str) -> Any | None:
        try:
            raw = self._l2.get(f"mia:cache:{key}")
            if raw is None:
                return None
            from storage.serializer import Serializer
            return Serializer.deserialize(raw)
        except Exception as e:
            log.debug("L2 get failed", extra={"key": key, "error": str(e)})
            return None

    def _set_l2(self, key: str, value: Any, ttl: int | None = None) -> None:
        try:
            from storage.serializer import Serializer
            serialized = Serializer.serialize(value)
            effective_ttl = ttl if ttl is not None else self._default_ttl
            self._l2.setex(f"mia:cache:{key}", effective_ttl, serialized)
        except Exception as e:
            log.debug("L2 set failed", extra={"key": key, "error": str(e)})

    def _delete_l2(self, key: str) -> bool:
        try:
            return bool(self._l2.delete(f"mia:cache:{key}"))
        except Exception as e:
            log.debug("L2 delete failed", extra={"key": key, "error": str(e)})
            return False

    def _exists_l2(self, key: str) -> bool:
        try:
            return bool(self._l2.exists(f"mia:cache:{key}"))
        except Exception:
            return False

    def _clear_l2(self) -> None:
        try:
            keys = self._l2.keys("mia:cache:*")
            if keys:
                self._l2.delete(*keys)
        except Exception as e:
            log.debug("L2 clear failed", extra={"error": str(e)})

    # ── Stats ───────────────────────────────────────

    def _record_hit(self) -> None:
        with self._stats_lock:
            self._hits += 1

    def _record_miss(self) -> None:
        with self._stats_lock:
            self._misses += 1
