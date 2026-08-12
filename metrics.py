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
