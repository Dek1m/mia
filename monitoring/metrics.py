"""Prometheus метрики для State Manager.

Метрики именуются по компонентам (слоям), а не по проекту.

Префиксы:
  - api_        — API Proxy
  - cache_      — CacheHierarchy
  - database_   — Database
  - task_       — Universal Task System
"""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, start_http_server
from argenta_logging import get_logger

log = get_logger(__name__)


# ============================================================
# API Proxy — вызовы методов
# ============================================================

api_calls_total = Counter(
    "api_calls_total",
    "Total API method calls",
    ["module", "method", "status"],
)

api_duration_seconds = Histogram(
    "api_duration_seconds",
    "API method call duration",
    ["module", "method"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)


# ============================================================
# Database — операции и кеш
# ============================================================

database_operations_total = Counter(
    "database_operations_total",
    "Total database operations",
    ["operation", "status"],
)

database_operation_duration_seconds = Histogram(
    "database_operation_duration_seconds",
    "Database operation duration",
    ["operation"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

database_cache_hits_total = Counter(
    "database_cache_hits_total",
    "Total database cache hits",
    ["level"],
)

database_cache_misses_total = Counter(
    "database_cache_misses_total",
    "Total database cache misses",
)


# ============================================================
# Universal Task System — метрики задач
# ============================================================

task_created_total = Counter(
    "task_created_total",
    "Total tasks created",
    ["module", "task_type"],
)

task_completed_total = Counter(
    "task_completed_total",
    "Total tasks completed",
    ["module", "task_type", "status"],
)

task_duration_seconds = Histogram(
    "task_duration_seconds",
    "Task execution duration",
    ["module", "task_type"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)


# ============================================================
# Cache Hierarchy — многоуровневый кеш
# ============================================================

cache_l0_size = Gauge(
    "cache_l0_size",
    "Current number of entries in L0 cache",
)

cache_l1_active = Gauge(
    "cache_l1_active",
    "Whether L1 cache layer is active (1 = active)",
)

cache_l2_active = Gauge(
    "cache_l2_active",
    "Whether L2 cache layer is active (1 = active)",
)


# ============================================================
# MetricsServer
# ============================================================


class MetricsServer:
    """HTTP сервер для Prometheus скрапинга."""

    def __init__(self, port: int = 9090) -> None:
        self._port = port

    def start(self) -> None:
        """Запустить сервер в отдельном потоке."""
        start_http_server(self._port)
        log.info("Metrics server started", extra={"port": self._port})

    def stop(self) -> None:
        """Остановить сервер."""
        log.info("Metrics server stopped")
