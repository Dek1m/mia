"""Prometheus метрики для State Manager.

Метрики именуются по компонентам (слоям), а не по проекту,
поскольку библиотека встраивается в другие проекты.

Префиксы:
  - state_      — State Manager (оркестрация)
  - api_        — API Proxy (вызовы методов)
  - threadpool_ — ThreadPoolManager
  - processpool_— ProcessPool
  - heartbeat_  — HeartbeatMonitor
  - module_     — ModuleManager (загрузка модулей)
  - memory_     — SharedMemoryManager
  - cpu_        — CpuMetricsCollector, CpuAffinityProvider
  - loadbalancer_ — LoadBalancer
  - worker_manager_ — WorkerManager
"""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, start_http_server
from argenta_logging import get_logger
import threading

log = get_logger(__name__)


# ============================================================
# State Manager — оркестрация
# ============================================================

state_module_loads_total = Counter(
    "state_module_loads_total",
    "Total module load attempts",
    ["module", "status"],
)

state_shutdowns_total = Counter(
    "state_shutdowns_total",
    "Total shutdown invocations",
)


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
# ThreadPool — потоки
# ============================================================

threadpool_active = Gauge(
    "threadpool_active",
    "Active threads in pool",
)

threadpool_tasks_submitted_total = Counter(
    "threadpool_tasks_submitted_total",
    "Total tasks submitted to thread pool",
    ["status"],
)


# ============================================================
# ProcessPool — процессы
# ============================================================

processpool_active = Gauge(
    "processpool_active",
    "Active worker processes",
)

processpool_spawned_total = Counter(
    "processpool_spawned_total",
    "Total processes spawned",
)

processpool_killed_total = Counter(
    "processpool_killed_total",
    "Total processes killed",
)

processpool_tasks_submitted_total = Counter(
    "processpool_tasks_submitted_total",
    "Total tasks submitted to process pool",
    ["status"],
)


# ============================================================
# Heartbeat — мониторинг
# ============================================================

heartbeat_missed_total = Counter(
    "heartbeat_missed_total",
    "Total missed heartbeats",
)

heartbeat_restarts_total = Counter(
    "heartbeat_restarts_total",
    "Total process restarts triggered by heartbeat",
)


# ============================================================
# Memory — shared memory
# ============================================================

memory_segments_active = Gauge(
    "memory_segments_active",
    "Active shared memory segments",
)

memory_bytes_allocated = Gauge(
    "memory_bytes_allocated",
    "Total bytes allocated in shared memory",
    ["segment"],
)


# ============================================================
# CPU Metrics — сбор метрик CPU
# ============================================================

cpu_load_gauge = Gauge(
    "cpu_load_gauge",
    "Overall CPU load (0.0 - 1.0)",
)

cpu_per_core_load_gauge = Gauge(
    "cpu_per_core_load_gauge",
    "Per-core CPU load (0.0 - 1.0)",
    ["core"],
)


# ============================================================
# Load Balancer — балансировка нагрузки
# ============================================================

loadbalancer_score_histogram = Histogram(
    "loadbalancer_score_histogram",
    "Worker selection score distribution",
    buckets=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
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
