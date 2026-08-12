"""Factories — фабрики для создания компонентов."""
from __future__ import annotations

from typing import Any
from argenta_logging import get_logger
from interfaces import ICache, IThreadPool, IProcessPool, IEventBus, IHeartbeatMonitor

log = get_logger(__name__)


class CacheFactory:
    """Фабрика кеш-бэкендов."""
    
    @staticmethod
    def create(backend: str = "null", **kwargs: Any) -> ICache:
        if backend == "null":
            from cache_interface import NullCache
            return NullCache()
        raise ValueError(f"Unknown cache backend: {backend}")


class ThreadPoolFactory:
    """Фабрика пулов потоков."""
    
    @staticmethod
    def create(max_workers: int | None = None) -> IThreadPool:
        from thread_pool import ThreadPoolManager
        return ThreadPoolManager(max_workers=max_workers)


class ProcessPoolFactory:
    """Фабрика пулов процессов."""
    
    @staticmethod
    def create(num_processes: int | None = None, **kwargs: Any) -> IProcessPool:
        from process_pool import ProcessPool
        return ProcessPool(num_processes=num_processes, **kwargs)


class EventBusFactory:
    """Фабрика шин событий."""
    
    @staticmethod
    def create() -> IEventBus:
        from event_bus import EventBus
        return EventBus()


class HeartbeatFactory:
    """Фабрика мониторов heartbeat."""
    
    @staticmethod
    def create(timeout: float = 30.0, check_interval: float = 5.0) -> IHeartbeatMonitor:
        from heartbeat_monitor import HeartbeatMonitor
        return HeartbeatMonitor(timeout=timeout, check_interval=check_interval)
