"""Unit-тесты для CacheHierarchy."""
from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from storage.cache_hierarchy import CacheHierarchy


# === Фикстуры ===

@pytest.fixture
def cache() -> CacheHierarchy:
    """L0-only кеш (без L1/L2)."""
    return CacheHierarchy(default_ttl=2)


@pytest.fixture
def cache_with_l2() -> CacheHierarchy:
    """Кеш с мокнутым Redis (L2)."""
    redis_mock = MagicMock()
    redis_mock.get.return_value = None
    redis_mock.delete.return_value = 0
    redis_mock.exists.return_value = 0
    redis_mock.keys.return_value = []
    return CacheHierarchy(l2_redis=redis_mock, default_ttl=60)


# === L0-only тесты ===

def test_set_get_l0(cache: CacheHierarchy) -> None:
    cache.set("k1", "v1")
    assert cache.get("k1") == "v1"


def test_get_miss(cache: CacheHierarchy) -> None:
    assert cache.get("missing") is None


def test_ttl_expiry(cache: CacheHierarchy) -> None:
    cache.set("k1", "v1", ttl=0)
    time.sleep(0.05)
    assert cache.get("k1") is None


def test_delete_l0(cache: CacheHierarchy) -> None:
    cache.set("k1", "v1")
    assert cache.delete("k1") is True
    assert cache.get("k1") is None


def test_delete_missing(cache: CacheHierarchy) -> None:
    assert cache.delete("missing") is False


def test_exists_l0(cache: CacheHierarchy) -> None:
    cache.set("k1", "v1")
    assert cache.exists("k1") is True
    assert cache.exists("missing") is False


def test_exists_expired(cache: CacheHierarchy) -> None:
    cache.set("k1", "v1", ttl=0)
    time.sleep(0.05)
    assert cache.exists("k1") is False


def test_clear_l0(cache: CacheHierarchy) -> None:
    cache.set("k1", "v1")
    cache.set("k2", "v2")
    cache.clear()
    assert cache.get("k1") is None
    assert cache.get("k2") is None


def test_overwrite_l0(cache: CacheHierarchy) -> None:
    cache.set("k1", "old")
    cache.set("k1", "new")
    assert cache.get("k1") == "new"


def test_stats_initial(cache: CacheHierarchy) -> None:
    s = cache.stats()
    assert s["hits"] == 0
    assert s["misses"] == 0
    assert s["hit_rate"] == 0.0
    assert s["l0_size"] == 0
    assert s["l1_active"] is False
    assert s["l2_active"] is False


def test_stats_hits_misses(cache: CacheHierarchy) -> None:
    cache.set("k1", "v1")
    cache.get("k1")  # hit
    cache.get("k2")  # miss
    s = cache.stats()
    assert s["hits"] == 1
    assert s["misses"] == 1
    assert s["hit_rate"] == 0.5


def test_l0_size_tracking(cache: CacheHierarchy) -> None:
    cache.set("k1", "v1")
    cache.set("k2", "v2")
    assert cache.stats()["l0_size"] == 2


# === L2 (Redis) тесты ===

def test_get_falls_through_to_l2(cache_with_l2: CacheHierarchy) -> None:
    from storage.serializer import Serializer
    redis_mock = cache_with_l2._l2
    redis_mock.get.return_value = Serializer.serialize("from_redis")

    result = cache_with_l2.get("k1")
    assert result == "from_redis"
    redis_mock.get.assert_called_once_with("mia:cache:k1")


def test_set_writes_to_l2(cache_with_l2: CacheHierarchy) -> None:
    from storage.serializer import Serializer
    cache_with_l2.set("k1", "v1", ttl=30)
    cache_with_l2._l2.setex.assert_called_once_with(
        "mia:cache:k1", 30, Serializer.serialize("v1")
    )


def test_delete_removes_from_l2(cache_with_l2: CacheHierarchy) -> None:
    cache_with_l2._l2.delete.return_value = 1
    result = cache_with_l2.delete("k1")
    assert result is True
    cache_with_l2._l2.delete.assert_called_once_with("mia:cache:k1")


def test_l2_hit_promotes_to_l0(cache_with_l2: CacheHierarchy) -> None:
    from storage.serializer import Serializer
    redis_mock = cache_with_l2._l2
    redis_mock.get.return_value = Serializer.serialize("from_redis")

    cache_with_l2.get("k1")
    # Второй запрос — уже из L0
    redis_mock.reset_mock()
    result = cache_with_l2.get("k1")
    assert result == "from_redis"
    redis_mock.get.assert_not_called()


def test_l2_stats(cache_with_l2: CacheHierarchy) -> None:
    from storage.serializer import Serializer
    cache_with_l2._l2.get.return_value = Serializer.serialize("v")
    cache_with_l2.get("k1")
    s = cache_with_l2.stats()
    assert s["l2_active"] is True
    assert s["hits"] == 1


def test_l2_exception_returns_none(cache_with_l2: CacheHierarchy) -> None:
    cache_with_l2._l2.get.side_effect = ConnectionError("redis down")
    assert cache_with_l2.get("k1") is None
    s = cache_with_l2.stats()
    assert s["misses"] == 1


def test_clear_includes_l2(cache_with_l2: CacheHierarchy) -> None:
    cache_with_l2._l2.keys.return_value = [b"mia:cache:k1", b"mia:cache:k2"]
    cache_with_l2.clear()
    cache_with_l2._l2.delete.assert_called_once_with(b"mia:cache:k1", b"mia:cache:k2")


# === NullCache compatibility ===

def test_null_cache_interface() -> None:
    from storage.cache_interface import NullCache
    nc = NullCache()
    assert nc.get("k") is None
    nc.set("k", "v")
    assert nc.delete("k") is False
    assert nc.exists("k") is False
    nc.clear()


# === CacheFactory ===

def test_factory_null() -> None:
    from core.factories import CacheFactory
    cache = CacheFactory.create("null")
    assert cache.get("k") is None


def test_factory_hierarchy() -> None:
    from core.factories import CacheFactory
    cache = CacheFactory.create("hierarchy", default_ttl=60)
    cache.set("k", "v")
    assert cache.get("k") == "v"


def test_factory_unknown_raises() -> None:
    from core.factories import CacheFactory
    with pytest.raises(ValueError, match="Unknown cache backend"):
        CacheFactory.create("nonexistent")
