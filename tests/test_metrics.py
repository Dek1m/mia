"""Unit-тесты для metrics.py."""
from prometheus_client import Counter, Gauge, Histogram

from monitoring.metrics import (
    MetricsServer,
    api_calls_total,
    api_duration_seconds,
    cache_l0_size,
    task_created_total,
)


def test_counter_increment():
    before = api_calls_total.labels(module="test", method="foo", status="ok")._value.get()
    api_calls_total.labels(module="test", method="foo", status="ok").inc()
    after = api_calls_total.labels(module="test", method="foo", status="ok")._value.get()
    assert after == before + 1


def test_counter_increment_value():
    before = task_created_total.labels(module="t", task_type="cpu")._value.get()
    task_created_total.labels(module="t", task_type="cpu").inc(5)
    after = task_created_total.labels(module="t", task_type="cpu")._value.get()
    assert after == before + 5


def test_histogram_observe():
    hist = api_duration_seconds.labels(module="test_hist", method="call")
    hist.observe(0.05)
    assert hist._sum.get() >= 0.05


def test_histogram_multiple_observations():
    hist = api_duration_seconds.labels(module="test_multi", method="call")
    hist.observe(0.01)
    hist.observe(0.05)
    hist.observe(0.1)
    assert hist._sum.get() >= 0.15


def test_gauge_set():
    cache_l0_size.set(42)
    assert cache_l0_size._value.get() == 42


def test_gauge_inc_dec():
    cache_l0_size.set(0)
    cache_l0_size.inc()
    assert cache_l0_size._value.get() == 1
    cache_l0_size.inc()
    assert cache_l0_size._value.get() == 2
    cache_l0_size.dec()
    assert cache_l0_size._value.get() == 1


def test_metrics_server_start():
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        port = s.getsockname()[1]
    server = MetricsServer(port=port)
    server.start()
    server.stop()


def test_metrics_server_creation():
    server = MetricsServer(port=19091)
    assert server._port == 19091
    assert server._thread is None


def test_all_metrics_exist():
    metrics = {
        "api_calls_total": (Counter, api_calls_total),
        "api_duration_seconds": (Histogram, api_duration_seconds),
        "task_created_total": (Counter, task_created_total),
        "cache_l0_size": (Gauge, cache_l0_size),
    }
    for name, (expected_type, metric) in metrics.items():
        assert metric is not None, f"Метрика {name} не определена"
        assert isinstance(metric, expected_type), (
            f"Метрика {name}: ожидался {expected_type.__name__}, "
            f"получен {type(metric).__name__}"
        )


def test_metric_prefixes():
    expected_prefixes = ["api_", "task_", "cache_"]
    all_metric_names = [
        api_calls_total._name,
        api_duration_seconds._name,
        task_created_total._name,
        cache_l0_size._name,
    ]
    for name in all_metric_names:
        has_prefix = any(name.startswith(p) for p in expected_prefixes)
        assert has_prefix, f"Метрика {name} не имеет известного префикса"
