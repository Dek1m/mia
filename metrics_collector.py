"""MetricsCollector — единый класс для метрик."""
from __future__ import annotations

from typing import Any
from argenta_logging import get_logger
from prometheus_client import Counter, Gauge, Histogram, start_http_server
import threading

log = get_logger(__name__)


class MetricsCollector:
    """Собирает все метрики в единый класс."""
    
    def __init__(self, prefix: str = "") -> None:
        self._prefix = prefix
        
        # API
        self.api_calls = Counter(
            f"{prefix}api_calls_total", "Total API calls",
            ["module", "method", "status"]
        )
        self.api_duration = Histogram(
            f"{prefix}api_duration_seconds", "API call duration",
            ["module", "method"],
            buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
        )
        
        # ThreadPool
        self.threadpool_active = Gauge(f"{prefix}threadpool_active", "Active threads")
        
        # ProcessPool
        self.processpool_active = Gauge(f"{prefix}processpool_active", "Active processes")
        self.processpool_spawned = Counter(f"{prefix}processpool_spawned_total", "Spawned processes")
        self.processpool_killed = Counter(f"{prefix}processpool_killed_total", "Killed processes")
        
        # Heartbeat
        self.heartbeat_missed = Counter(f"{prefix}heartbeat_missed_total", "Missed heartbeats")
        
        # Modules
        self.module_loads = Counter(
            f"{prefix}module_loads_total", "Module loads",
            ["module", "status"]
        )
        
        # Cache
        self.cache_hits = Counter(f"{prefix}cache_hits_total", "Cache hits", ["backend"])
        self.cache_misses = Counter(f"{prefix}cache_misses_total", "Cache misses")
    
    def start_server(self, port: int = 9090) -> None:
        """Запустить HTTP сервер для Prometheus."""
        start_http_server(port)
        log.info("Metrics server started", extra={"port": port})
