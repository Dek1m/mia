"""Factories — фабрики для создания компонентов."""
from __future__ import annotations

from typing import Any

from argenta_logging import get_logger
from core.interfaces import ICache, IDatabase, IEventBus

log = get_logger(__name__)


def _cfg() -> Any:
    from core.config import MiaConfig

    return MiaConfig.get()


class CacheFactory:
    """Фабрика кеш-бэкендов.

    backends:
        null      — NullCache
        hierarchy — CacheHierarchy (L0 dict → L1 SharedMemory → L2 Redis)
    """

    @staticmethod
    def create(backend: str | None = None, **kwargs: Any) -> ICache:
        if backend is None:
            backend = _cfg().get_value("storage.cache.backend", "null")
        if backend == "null":
            from storage.cache_interface import NullCache

            return NullCache()
        if backend == "hierarchy":
            from storage.cache_hierarchy import CacheHierarchy

            return CacheHierarchy(
                l1_shm=kwargs.get("l1_shm"),
                l1_segment=kwargs.get("l1_segment", "cache_l1"),
                l1_size=kwargs.get("l1_size", 4 * 1024 * 1024),
                l2_redis=kwargs.get("l2_redis"),
                default_ttl=kwargs.get("default_ttl", 300),
            )
        raise ValueError(f"Unknown cache backend: {backend}")


class EventBusFactory:
    """Фабрика шин событий."""

    @staticmethod
    def create() -> IEventBus:
        from communication.event_bus import EventBus

        return EventBus()


class DatabaseFactory:
    """Фабрика Database."""

    @staticmethod
    def create(cache: Any | None = None, dispatcher: Any | None = None) -> IDatabase:
        from core.database import Database

        return Database(cache=cache, dispatcher=dispatcher)

    @staticmethod
    def create_with_task_system(
        cache: Any | None = None,
        dispatcher: Any | None = None,
    ) -> tuple[IDatabase, Any, Any]:
        """Создать Database + StatsBatchWriter.

        Returns:
            (database, task_store, stats_writer)
        """
        from core.database import Database
        from core.stats_batch_writer import StatsBatchWriter

        database = Database(cache=cache, dispatcher=dispatcher, stats_writer=None)
        stats_writer = StatsBatchWriter(db=database)
        database._stats_writer = stats_writer
        return database, None, stats_writer
