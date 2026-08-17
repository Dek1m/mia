"""Unit-тесты обратной совместимости @db_method и SmartDispatcher.

Гарантии:
  - Все атрибуты _db_* доступны через getattr
  - @db_method принимает все старые параметры
  - Существующий код не ломается
"""
from __future__ import annotations

import asyncio
from concurrent.futures import Future
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.task import Task, TaskStatus, TaskType
from core.task_store import TaskStore
from core.task_decorator import set_global_dispatcher
from modules.db.provider import db_method, DatabaseProvider
from pools.smart_dispatcher import SmartDispatcher


# ── Вспомогательные классы ──────────────────────────


class FakeCache:
    """Минимальная имитация кеша (dict + TTL)."""

    def __init__(self) -> None:
        self._store: dict[str, Any] = {}

    def get(self, key: str) -> Any | None:
        return self._store.get(key)

    def set(self, key: str, value: Any, ttl: int = 0) -> None:
        self._store[key] = value


class FakeProvider:
    """Минимальный провайдер с кешем и мокнутым pool."""

    def __init__(self, cache: FakeCache | None = None) -> None:
        self._cache = cache
        self._pool = AsyncMock()


class FakeWorkerManager:
    """Заглушка WorkerManager для тестов."""

    def __init__(self):
        self.submitted = []

    def submit(self, fn, *args, **kwargs):
        self.submitted.append((fn, args, kwargs))
        return fn(*args, **kwargs)


@pytest.fixture(autouse=True)
def _setup_dispatcher():
    """Установить мок SmartDispatcher для всех тестов."""
    wm = FakeWorkerManager()
    dp = SmartDispatcher(wm)
    set_global_dispatcher(dp)
    yield
    set_global_dispatcher(None)


# ============================================================
# 1. Все _db_* атрибуты доступны через getattr
# ============================================================


class TestAllDbAttributesAccessible:
    """Все атрибуты _db_* доступны через getattr на wrapper."""

    REQUIRED_ATTRS = [
        "_db_type",
        "_db_timeout",
        "_db_cache_ttl",
        "_db_cache_key",
        "_db_lock",
        "_db_lock_timeout",
        "_db_validate",
        "_db_audit",
        "_db_retry",
        "_db_retry_delay",
        "_db_metrics",
    ]

    # Атрибуты, которые могут быть None по умолчанию
    NONEABLE_ATTRS = {"_db_cache_key", "_db_lock", "_db_validate"}

    def test_all_attrs_present_with_defaults(self) -> None:
        """@db_method() без аргументов — все атрибуты доступны."""

        @db_method()
        async def get(self, id: str) -> dict:
            return {"id": id}

        for attr in self.REQUIRED_ATTRS:
            assert hasattr(get, attr), f"Отсутствует атрибут {attr}"
            if attr not in self.NONEABLE_ATTRS:
                assert getattr(get, attr) is not None, (
                    f"Атрибут {attr} не должен быть None"
                )

    def test_all_attrs_present_with_explicit_values(self) -> None:
        """@db_method(...) со всеми аргументами — все атрибуты доступны."""

        @db_method(
            type="write",
            timeout=15.0,
            cache_ttl=60,
            cache_key="item:{id}",
            lock="lock:{id}",
            lock_timeout=10.0,
            validate=None,
            audit=True,
            retry=3,
            retry_delay=1.0,
            metrics="custom.metric",
        )
        async def put(self, id: str) -> dict:
            return {"id": id}

        for attr in self.REQUIRED_ATTRS:
            assert hasattr(put, attr), f"Отсутствует атрибут {attr}"

    def test_db_type_read(self) -> None:
        @db_method(type="read")
        async def get(self) -> None:
            pass
        assert getattr(get, "_db_type") == "read"

    def test_db_type_write(self) -> None:
        @db_method(type="write")
        async def put(self) -> None:
            pass
        assert getattr(put, "_db_type") == "write"

    def test_db_type_aggregate(self) -> None:
        @db_method(type="aggregate")
        async def agg(self) -> None:
            pass
        assert getattr(agg, "_db_type") == "aggregate"

    def test_db_type_transaction(self) -> None:
        @db_method(type="transaction")
        async def txn(self) -> None:
            pass
        assert getattr(txn, "_db_type") == "transaction"

    def test_db_timeout(self) -> None:
        @db_method(timeout=25.0)
        async def slow(self) -> None:
            pass
        assert getattr(slow, "_db_timeout") == 25.0

    def test_db_cache_ttl(self) -> None:
        @db_method(cache_ttl=120)
        async def cached(self) -> None:
            pass
        assert getattr(cached, "_db_cache_ttl") == 120

    def test_db_cache_key(self) -> None:
        @db_method(cache_key="user:{id}")
        async def keyed(self) -> None:
            pass
        assert getattr(keyed, "_db_cache_key") == "user:{id}"

    def test_db_lock(self) -> None:
        @db_method(lock="lock:{table}:{id}")
        async def locked(self) -> None:
            pass
        assert getattr(locked, "_db_lock") == "lock:{table}:{id}"

    def test_db_lock_timeout(self) -> None:
        @db_method(lock_timeout=15.0)
        async def lt(self) -> None:
            pass
        assert getattr(lt, "_db_lock_timeout") == 15.0

    def test_db_validate_none(self) -> None:
        @db_method(validate=None)
        async def v(self) -> None:
            pass
        assert getattr(v, "_db_validate") is None

    def test_db_audit(self) -> None:
        @db_method(audit=True)
        async def audited(self) -> None:
            pass
        assert getattr(audited, "_db_audit") is True

    def test_db_retry(self) -> None:
        @db_method(retry=5)
        async def retried(self) -> None:
            pass
        assert getattr(retried, "_db_retry") == 5

    def test_db_retry_delay(self) -> None:
        @db_method(retry_delay=2.5)
        async def delayed(self) -> None:
            pass
        assert getattr(delayed, "_db_retry_delay") == 2.5

    def test_db_metrics_custom(self) -> None:
        @db_method(metrics="my.metric.name")
        async def measured(self) -> None:
            pass
        assert getattr(measured, "_db_metrics") == "my.metric.name"

    def test_db_metrics_default(self) -> None:
        @db_method()
        async def default_metrics(self) -> None:
            pass
        assert getattr(default_metrics, "_db_metrics") == "db.default_metrics"


# ============================================================
# 2. @db_method принимает все старые параметры
# ============================================================


class TestDbMethodAcceptsAllLegacyParams:
    """@db_method принимает все legacy-параметры без ошибок."""

    def test_all_params_accepted(self) -> None:
        """Все legacy-параметры передаются без ошибок."""

        @db_method(
            type="read",
            timeout=10.0,
            cache_ttl=30,
            cache_key="item:{id}",
            lock="lock:{id}",
            lock_timeout=5.0,
            validate=None,
            audit=False,
            retry=2,
            retry_delay=0.5,
            metrics="db.test",
        )
        async def get_item(self, id: str) -> dict:
            return {"id": id}

        provider = FakeProvider()
        result = asyncio.run(get_item(provider, "42"))
        assert result == {"id": "42"}

    def test_minimal_params(self) -> None:
        """Минимальный набор параметров."""

        @db_method()
        async def minimal(self) -> None:
            pass

        assert getattr(minimal, "_db_type") == "read"
        assert getattr(minimal, "_db_timeout") == 10.0
        assert getattr(minimal, "_db_cache_ttl") == 0
        assert getattr(minimal, "_db_cache_key") is None
        assert getattr(minimal, "_db_lock") is None
        assert getattr(minimal, "_db_lock_timeout") == 5.0
        assert getattr(minimal, "_db_validate") is None
        assert getattr(minimal, "_db_audit") is False
        assert getattr(minimal, "_db_retry") == 0
        assert getattr(minimal, "_db_retry_delay") == 0.5
        assert getattr(minimal, "_db_metrics") == "db.minimal"

    def test_only_type_param(self) -> None:
        @db_method(type="write")
        async def write_only(self) -> None:
            pass
        assert getattr(write_only, "_db_type") == "write"
        assert getattr(write_only, "_db_timeout") == 10.0

    def test_only_timeout_param(self) -> None:
        @db_method(timeout=30.0)
        async def timeout_only(self) -> None:
            pass
        assert getattr(timeout_only, "_db_type") == "read"
        assert getattr(timeout_only, "_db_timeout") == 30.0

    def test_cache_params(self) -> None:
        @db_method(cache_ttl=60, cache_key="user:{id}")
        async def cached(self, id: str) -> dict:
            return {"id": id}
        assert getattr(cached, "_db_cache_ttl") == 60
        assert getattr(cached, "_db_cache_key") == "user:{id}"

    def test_lock_params(self) -> None:
        @db_method(lock="lock:{id}", lock_timeout=15.0)
        async def locked(self, id: str) -> dict:
            return {"id": id}
        assert getattr(locked, "_db_lock") == "lock:{id}"
        assert getattr(locked, "_db_lock_timeout") == 15.0

    def test_retry_params(self) -> None:
        @db_method(retry=5, retry_delay=2.0)
        async def retried(self) -> None:
            pass
        assert getattr(retried, "_db_retry") == 5
        assert getattr(retried, "_db_retry_delay") == 2.0


# ============================================================
# 3. Существующий код не ломается
# ============================================================


class TestExistingCodeNotBroken:
    """Интеграционные тесты: существующий код продолжает работать."""

    def test_caching_works(self) -> None:
        """Кеш работает через @db_method."""
        cache = FakeCache()

        @db_method(cache_ttl=60, cache_key="get:{id}")
        async def get(self, id: str) -> dict:
            return {"id": id, "fresh": True}

        provider = FakeProvider(cache=cache)
        result = asyncio.run(get(provider, "42"))
        assert result == {"id": "42", "fresh": True}
        cached = cache.get("get:42")
        assert cached == {"id": "42", "fresh": True}

    def test_database_provider_methods_have_all_attrs(self) -> None:
        """Методы DatabaseProvider имеют все _db_* атрибуты."""
        provider = DatabaseProvider(pool=AsyncMock(), config=MagicMock())

        methods = [
            "get", "get_by_field", "list", "insert", "update",
            "delete", "exists", "count", "fetch", "execute",
            "bulk_insert", "bulk_update", "bulk_delete",
        ]

        for method_name in methods:
            method = getattr(provider, method_name)
            for attr in TestAllDbAttributesAccessible.REQUIRED_ATTRS:
                assert hasattr(method, attr), (
                    f"{method_name} не имеет атрибута {attr}"
                )

    def test_database_provider_get_type_mapping(self) -> None:
        """DatabaseProvider.get: type='read' → _db_type='read'."""
        provider = DatabaseProvider(pool=AsyncMock(), config=MagicMock())
        assert getattr(provider.get, "_db_type") == "read"
        assert getattr(provider.get, "_task_type") == TaskType.IO

    def test_database_provider_insert_type_mapping(self) -> None:
        """DatabaseProvider.insert: type='write' → _db_type='write'."""
        provider = DatabaseProvider(pool=AsyncMock(), config=MagicMock())
        assert getattr(provider.insert, "_db_type") == "write"
        assert getattr(provider.insert, "_task_type") == TaskType.IO

    def test_database_provider_count_type_mapping(self) -> None:
        """DatabaseProvider.count: type='aggregate' → _db_type='aggregate'."""
        provider = DatabaseProvider(pool=AsyncMock(), config=MagicMock())
        assert getattr(provider.count, "_db_type") == "aggregate"
        assert getattr(provider.count, "_task_type") == TaskType.AGGREGATE

    def test_database_provider_transaction_type_mapping(self) -> None:
        """DatabaseProvider.transaction: type='transaction' → _db_type='transaction'."""
        provider = DatabaseProvider(pool=AsyncMock(), config=MagicMock())
        assert getattr(provider.transaction, "_db_type") == "transaction"
        assert getattr(provider.transaction, "_task_type") == TaskType.DATABASE
