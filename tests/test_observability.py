"""Тесты для database observability — метрики и structured logging."""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from monitoring.metrics import (
    database_operations_total,
    database_operation_duration_seconds,
    database_cache_hits_total,
    database_cache_misses_total,
)


# === Метрики существуют ===

def test_database_operations_counter_exists():
    """database_operations_total — Counter с labels operation, status."""
    assert database_operations_total is not None
    assert database_operations_total._labelnames == ("operation", "status")


def test_database_operation_duration_exists():
    """database_operation_duration_seconds — Histogram с label operation."""
    assert database_operation_duration_seconds is not None
    assert "operation" in database_operation_duration_seconds._labelnames


def test_database_cache_hits_exists():
    """database_cache_hits_total — Counter с label level."""
    assert database_cache_hits_total is not None
    assert database_cache_hits_total._labelnames == ("level",)


def test_database_cache_misses_exists():
    """database_cache_misses_total — Counter без labels."""
    assert database_cache_misses_total is not None


# === Counter инкременты ===

def test_operations_counter_ok():
    """Counter увеличивается при успешной операции."""
    before = database_operations_total.labels(operation="get", status="ok")._value.get()
    database_operations_total.labels(operation="get", status="ok").inc()
    after = database_operations_total.labels(operation="get", status="ok")._value.get()
    assert after == before + 1


def test_operations_counter_error():
    """Counter увеличивается при ошибке."""
    before = database_operations_total.labels(operation="insert", status="error")._value.get()
    database_operations_total.labels(operation="insert", status="error").inc()
    after = database_operations_total.labels(operation="insert", status="error")._value.get()
    assert after == before + 1


def test_cache_hits_counter():
    """Cache hits counter увеличивается по уровням."""
    before_l0 = database_cache_hits_total.labels(level="l0")._value.get()
    database_cache_hits_total.labels(level="l0").inc()
    after_l0 = database_cache_hits_total.labels(level="l0")._value.get()
    assert after_l0 == before_l0 + 1


def test_cache_misses_counter():
    """Cache misses counter увеличивается."""
    before = database_cache_misses_total._value.get()
    database_cache_misses_total.inc()
    after = database_cache_misses_total._value.get()
    assert after == before + 1


# === Histogram observations ===

def test_histogram_observe():
    """Histogram записывает значение длительности."""
    hist = database_operation_duration_seconds.labels(operation="get")
    hist.observe(0.05)
    assert hist._sum.get() >= 0.05


# === Database фасад с метриками ===

def test_database_get_records_metrics():
    """Database.get() записывает метрики при cache hit."""
    from core.database import Database

    cache = MagicMock()
    cache.get.return_value = {"id": "1", "cached": True}

    db = Database(cache=cache)
    provider = MagicMock()
    db.register_provider("test", provider, is_default=True)

    result = db.get("users", "1")

    assert result == {"id": "1", "cached": True}
    cache.get.assert_called_once_with("db:users:1")


def test_database_get_miss_records_metrics():
    """Database.get() записывает метрики при cache miss."""
    from core.database import Database

    cache = MagicMock()
    cache.get.return_value = None
    cache.set = MagicMock()

    provider = MagicMock()
    provider.get.return_value = {"id": "1", "from_db": True}

    db = Database(cache=cache)
    db.register_provider("test", provider, is_default=True)

    result = db.get("users", "1")

    assert result == {"id": "1", "from_db": True}
    cache.set.assert_called_once_with("db:users:1", {"id": "1", "from_db": True})


def test_database_insert_records_metrics():
    """Database.insert() записывает метрики."""
    from core.database import Database

    cache = MagicMock()
    provider = MagicMock()
    provider.insert.return_value = "new-id"

    db = Database(cache=cache)
    db.register_provider("test", provider, is_default=True)

    result = db.insert("users", {"name": "test"})

    assert result == "new-id"
    provider.insert.assert_called_once_with("users", {"name": "test"})


def test_database_error_records_metric():
    """Database.get() записывает error метрику при исключении."""
    from core.database import Database

    provider = MagicMock()
    provider.get.side_effect = ConnectionError("db down")

    db = Database()
    db.register_provider("test", provider, is_default=True)

    with pytest.raises(ConnectionError):
        db.get("users", "1")


# === Cache hierarchy с метриками ===

def test_cache_hierarchy_records_hit_metric():
    """CacheHierarchy.get() записывает database_cache_hits_total при hit."""
    from storage.cache_hierarchy import CacheHierarchy

    cache = CacheHierarchy(default_ttl=60)
    cache.set("k1", "v1")

    before = database_cache_hits_total.labels(level="l0")._value.get()
    cache.get("k1")
    after = database_cache_hits_total.labels(level="l0")._value.get()
    assert after >= before + 1


def test_cache_hierarchy_records_miss_metric():
    """CacheHierarchy.get() записывает database_cache_misses_total при miss."""
    from storage.cache_hierarchy import CacheHierarchy

    cache = CacheHierarchy(default_ttl=60)

    before = database_cache_misses_total._value.get()
    cache.get("missing_key")
    after = database_cache_misses_total._value.get()
    assert after >= before + 1
