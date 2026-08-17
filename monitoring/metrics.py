"""Prometheus метрики для State Manager.

Метрики именуются по компонентам (слоям), а не по проекту,
поскольку библиотека встраивается в другие проекты.

Префиксы:
  - api_        — API Proxy (вызовы методов)
  - processpool_— ProcessPool
  - heartbeat_  — HeartbeatMonitor
  - cpu_        — CpuMetricsCollector, CpuAffinityProvider
  - loadbalancer_ — LoadBalancer
  - worker_manager_ — WorkerManager
  - dispatcher_ — SmartDispatcher
  - cache_      — CacheHierarchy
"""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, start_http_server
from argenta_logging import get_logger
import threading

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
# ProcessPool — процессы
# ============================================================

processpool_spawned_total = Counter(
    "processpool_spawned_total",
    "Total processes spawned",
)

processpool_killed_total = Counter(
    "processpool_killed_total",
    "Total processes killed",
)


# ============================================================
# Heartbeat — мониторинг
# ============================================================

heartbeat_missed_total = Counter(
    "heartbeat_missed_total",
    "Total missed heartbeats",
)


# ============================================================
# CPU Metrics — сбор метрик CPU
# ============================================================

cpu_load = Gauge(
    "cpu_load",
    "Overall CPU load (0.0 - 1.0)",
)

cpu_per_core_load = Gauge(
    "cpu_per_core_load",
    "Per-core CPU load (0.0 - 1.0)",
    ["core"],
)


# ============================================================
# Load Balancer — балансировка нагрузки
# ============================================================

loadbalancer_score = Histogram(
    "loadbalancer_score",
    "Worker selection score distribution",
    buckets=(0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
)

loadbalancer_selections_total = Counter(
    "loadbalancer_selections_total",
    "Total worker selections",
    ["worker_id"],
)

loadbalancer_no_worker_total = Counter(
    "loadbalancer_no_worker_total",
    "Total selections with no available worker",
)


# ============================================================
# Worker Manager — управление воркерами
# ============================================================

worker_manager_active = Gauge(
    "worker_manager_active",
    "Active workers managed by WorkerManager",
)

worker_manager_restarts_total = Counter(
    "worker_manager_restarts_total",
    "Total worker restarts",
)

worker_manager_tasks_submitted_total = Counter(
    "worker_manager_tasks_submitted_total",
    "Total tasks submitted through WorkerManager",
    ["status"],
)

worker_manager_task_duration_seconds = Histogram(
    "worker_manager_task_duration_seconds",
    "Task execution duration in WorkerManager",
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)


# ============================================================
# CPU Affinity — привязка к ядрам
# ============================================================

cpu_affinity_set_total = Counter(
    "cpu_affinity_set_total",
    "Total successful CPU affinity bindings",
)

cpu_affinity_errors_total = Counter(
    "cpu_affinity_errors_total",
    "Total CPU affinity binding errors",
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
# Dispatcher — маршрутизация задач
# ============================================================

dispatcher_dispatch_total = Counter(
    "dispatcher_dispatch_total",
    "Total dispatched tasks",
    ["pool", "db_type"],
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
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Запустить сервер в отдельном потоке."""
        start_http_server(self._port)
        log.info("Metrics server started", extra={"port": self._port})

    def stop(self) -> None:
        """Остановить сервер."""
        log.info("Metrics server stopped")
