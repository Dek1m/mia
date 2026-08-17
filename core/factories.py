"""Factories — фабрики для создания компонентов."""
from __future__ import annotations

from typing import Any
from argenta_logging import get_logger
from core.interfaces import (
    ICache, IEventBus, IHeartbeatMonitor,
    ICpuMetricsCollector, ILoadBalancer, IWorkerManager, IDatabase,
)

log = get_logger(__name__)


def _cfg() -> Any:
    """Ленивый импорт MiaConfig для избежания циклических импортов."""
    from core.config import MiaConfig
    return MiaConfig.get()


class CacheFactory:
    """Фабрика кеш-бэкендов.

    backends:
        null     — NullCache (заглушка)
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


class HeartbeatFactory:
    """Фабрика мониторов heartbeat."""

    @staticmethod
    def create(timeout: float | None = None, check_interval: float | None = None) -> IHeartbeatMonitor:
        from monitoring.heartbeat_monitor import HeartbeatMonitor
        cfg = _cfg()
        if timeout is None:
            timeout = cfg.get_value("monitoring.heartbeat.timeout", 30.0)
        if check_interval is None:
            check_interval = cfg.get_value("monitoring.heartbeat.check_interval", 5.0)
        return HeartbeatMonitor(timeout=timeout, check_interval=check_interval)


class CpuMetricsCollectorFactory:
    """Фабрика сборщиков метрик CPU."""

    @staticmethod
    def create(collect_interval: float | None = None) -> ICpuMetricsCollector:
        from pools.cpu_metrics import CpuMetricsCollector
        if collect_interval is None:
            collect_interval = _cfg().get_value("pools.cpu_metrics.collect_interval", 1.0)
        return CpuMetricsCollector(collect_interval=collect_interval)


class LoadBalancerFactory:
    """Фабрика балансировщиков нагрузки."""

    @staticmethod
    def create() -> ILoadBalancer:
        from pools.load_balancer import LoadBalancer
        return LoadBalancer()


class WorkerManagerFactory:
    """Фабрика менеджеров воркеров."""

    @staticmethod
    def create(
        load_balancer: Any | None = None,
        heartbeat_monitor: Any | None = None,
        shared_memory: Any | None = None,
    ) -> IWorkerManager:
        from pools.worker_manager import WorkerManager
        from pools.load_balancer import LoadBalancer
        from core.shared_memory import SharedMemoryManager
        return WorkerManager(
            load_balancer=load_balancer or LoadBalancer(),
            heartbeat_monitor=heartbeat_monitor,
            shared_memory=shared_memory or SharedMemoryManager(),
        )


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
        task_store: Any | None = None,
    ) -> tuple[IDatabase, Any, Any]:
        """Создать Database с подключённым Universal Task System.

        Args:
            cache: Кеш-бэкенд.
            dispatcher: SmartDispatcher.
            task_store: Готовый TaskStore. Если None — создаётся новый.

        Returns:
            (database, task_store, stats_writer)
        """
        from core.database import Database
        from core.task_store import TaskStore
        from core.stats_batch_writer import StatsBatchWriter

        if task_store is None:
            task_store = TaskStore()
        database = Database(
            cache=cache,
            dispatcher=dispatcher,
            task_store=task_store,
            stats_writer=None,  # циклическая зависимость — устанавливается ниже
        )
        stats_writer = StatsBatchWriter(db=database)
        # StatsBatchWriter теперь ссылается на database,
        # а database ссылается на stats_writer — замыкание корректно
        database._stats_writer = stats_writer
        return database, task_store, stats_writer
