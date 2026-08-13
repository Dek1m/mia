"""Unit-тесты для metrics.py."""
import time
from unittest.mock import patch, MagicMock

import pytest

from prometheus_client import Counter, Gauge, Histogram

from monitoring.metrics import (
    # API
    api_calls_total,
    api_duration_seconds,
    # Thread pool
    threadpool_active,
    threadpool_tasks_submitted_total,
    # Process pool
    processpool_spawned_total,
    processpool_killed_total,
    # Heartbeat
    heartbeat_missed_total,
    # Server
    MetricsServer,
)


# === Counter ===

def test_counter_increment():
    """Counter увеличивается при inc()."""
    before = api_calls_total.labels(module="test", method="foo", status="ok")._value.get()
    api_calls_total.labels(module="test", method="foo", status="ok").inc()
    after = api_calls_total.labels(module="test", method="foo", status="ok")._value.get()
    assert after == before + 1


def test_counter_increment_value():
    """Counter увеличивается на заданное значение."""
    before = heartbeat_missed_total._value.get()
    heartbeat_missed_total.inc(5)
    after = heartbeat_missed_total._value.get()
    assert after == before + 5


# === Histogram ===

def test_histogram_observe():
    """Histogram записывает значение."""
    hist = api_duration_seconds.labels(module="test_hist", method="call")
    hist.observe(0.05)

    # Проверяем что хотя бы один бакет содержит значение
    samples = {s.name: s.value for s in hist._children[0]._buckets} if hasattr(hist, '_children') else {}
    # Более надёжная проверка — посмотреть на sum/count
    assert hist._sum.get() >= 0.05


def test_histogram_multiple_observations():
    """Histogram записывает несколько значений."""
    hist = api_duration_seconds.labels(module="test_multi", method="call")
    hist.observe(0.01)
    hist.observe(0.05)
    hist.observe(0.1)

    # Проверяем sum — сумма всех observations
    assert hist._sum.get() >= 0.15


# === Gauge ===

def test_gauge_set():
    """Gauge устанавливает значение через set()."""
    threadpool_active.set(42)
    assert threadpool_active._value.get() == 42


def test_gauge_inc_dec():
    """Gauge увеличивается и уменьшается."""
    threadpool_active.set(0)
    threadpool_active.inc()
    assert threadpool_active._value.get() == 1
    threadpool_active.inc()
    assert threadpool_active._value.get() == 2
    threadpool_active.dec()
    assert threadpool_active._value.get() == 1


# === MetricsServer ===

def test_metrics_server_start():
    """MetricsServer запускается без ошибок."""
    import socket
    # Найти свободный порт
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        port = s.getsockname()[1]
    
    server = MetricsServer(port=port)
    server.start()
    server.stop()


def test_metrics_server_creation():
    """MetricsServer() создаётся с портом."""
    server = MetricsServer(port=19091)
    assert server._port == 19091
    assert server._thread is None


# === Все метрики существуют ===

def test_all_metrics_exist():
    """Все метрики из таблицы документации существуют и являются правильными типами."""
    metrics = {
        # API
        "api_calls_total": (Counter, api_calls_total),
        "api_duration_seconds": (Histogram, api_duration_seconds),
        # Thread pool
        "threadpool_active": (Gauge, threadpool_active),
        "threadpool_tasks_submitted_total": (Counter, threadpool_tasks_submitted_total),
        # Process pool
        "processpool_spawned_total": (Counter, processpool_spawned_total),
        "processpool_killed_total": (Counter, processpool_killed_total),
        # Heartbeat
        "heartbeat_missed_total": (Counter, heartbeat_missed_total),
    }

    for name, (expected_type, metric) in metrics.items():
        assert metric is not None, f"Метрика {name} не определена"
        assert isinstance(metric, expected_type), (
            f"Метрика {name}: ожидался {expected_type.__name__}, "
            f"получен {type(metric).__name__}"
        )


def test_metric_prefixes():
    """Все метрики имеют правильные префиксы слоёв."""
    expected_prefixes = [
        "api_",
        "threadpool_",
        "processpool_",
        "heartbeat_",
    ]

    # Собираем все имена метрик из модуля
    all_metric_names = [
        api_calls_total._name,
        api_duration_seconds._name,
        threadpool_active._name,
        threadpool_tasks_submitted_total._name,
        processpool_spawned_total._name,
        processpool_killed_total._name,
        heartbeat_missed_total._name,
    ]

    for name in all_metric_names:
        has_prefix = any(name.startswith(p) for p in expected_prefixes)
        assert has_prefix, f"Метрика {name} не имеет известного префикса"
