"""Factories — фабрики для создания компонентов."""
from __future__ import annotations

from typing import Any
from argenta_logging import get_logger
from core.interfaces import (
    ICache, IThreadPool, IEventBus, IHeartbeatMonitor,
    ICpuMetricsCollector, ILoadBalancer, IWorkerManager, IDatabase,
)

log = get_logger(__name__)


class CacheFactory:
    """Фабрика кеш-бэкендов.

    backends:
        null     — NullCache (заглушка)
        hierarchy — CacheHierarchy (L0 dict → L1 SharedMemory → L2 Redis)
    """

    @staticmethod
    def create(backend: str = "null", **kwargs: Any) -> ICache:
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


class ThreadPoolFactory:
    """Фабрика пулов потоков."""
    
    @staticmethod
    def create(max_workers: int | None = None) -> IThreadPool:
        from pools.thread_pool import ThreadPoolManager
        return ThreadPoolManager(max_workers=max_workers)


class EventBusFactory:
    """Фабрика шин событий."""
    
    @staticmethod
    def create() -> IEventBus:
        from communication.event_bus import EventBus
        return EventBus()


class HeartbeatFactory:
    """Фабрика мониторов heartbeat."""
    
    @staticmethod
    def create(timeout: float = 30.0, check_interval: float = 5.0) -> IHeartbeatMonitor:
        from monitoring.heartbeat_monitor import HeartbeatMonitor
        return HeartbeatMonitor(timeout=timeout, check_interval=check_interval)


class CpuMetricsCollectorFactory:
    """Фабрика сборщиков метрик CPU."""

    @staticmethod
    def create(collect_interval: float = 1.0) -> ICpuMetricsCollector:
        from pools.cpu_metrics import CpuMetricsCollector
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
    ) -> IWorkerManager:
        from pools.worker_manager import WorkerManager
        from pools.load_balancer import LoadBalancer
        return WorkerManager(
            load_balancer=load_balancer or LoadBalancer(),
            heartbeat_monitor=heartbeat_monitor,
        )


class DatabaseFactory:
    """Фабрика Database."""

    @staticmethod
    def create(cache: Any | None = None, dispatcher: Any | None = None) -> IDatabase:
        from core.database import Database
        return Database(cache=cache, dispatcher=dispatcher)
