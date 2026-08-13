"""Unit-тесты для декоратора @db_method."""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.task import TaskType
from modules.db.provider import db_method, _resolve_cache_key


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


# ── Тесты: метаданные ────────────────────────────────


def test_metadata_preserved() -> None:
    """Атрибуты декоратора доступны на wrapper."""

    @db_method(
        type="read",
        timeout=5.0,
        cache_ttl=30,
        cache_key="item:{id}",
        lock="lock:{id}",
        validate=None,
        retry=2,
        retry_delay=0.1,
        metrics="custom.metric",
    )
    async def get_item(self, id: str) -> dict:
        return {"id": id}

    assert get_item._db_type == "read"
    assert get_item._db_timeout == 5.0
    assert get_item._db_cache_ttl == 30
    assert get_item._db_cache_key == "item:{id}"
    assert get_item._db_lock == "lock:{id}"
    assert get_item._db_retry == 2
    assert get_item._db_retry_delay == 0.1
    assert get_item._db_metrics == "custom.metric"


def test_default_metrics() -> None:
    """Metrics по умолчанию = 'db.{func_name}'."""

    @db_method()
    async def my_method(self) -> None:
        pass

    assert my_method._db_metrics == "db.my_method"


# ── Тесты: маппинг типов DB → TaskType ─────────────────


def test_type_mapping_read_to_io() -> None:
    """type='read' → TaskType.IO."""

    @db_method(type="read")
    async def get(self) -> None:
        pass

    assert get._task_type == TaskType.IO
    assert get._db_type == "read"


def test_type_mapping_write_to_io() -> None:
    """type='write' → TaskType.IO."""

    @db_method(type="write")
    async def put(self) -> None:
        pass

    assert put._task_type == TaskType.IO
    assert put._db_type == "write"


def test_type_mapping_aggregate() -> None:
    """type='aggregate' → TaskType.AGGREGATE."""

    @db_method(type="aggregate")
    async def agg(self) -> None:
        pass

    assert agg._task_type == TaskType.AGGREGATE
    assert agg._db_type == "aggregate"


def test_type_mapping_transaction_to_database() -> None:
    """type='transaction' → TaskType.DATABASE."""

    @db_method(type="transaction")
    async def txn(self) -> None:
        pass

    assert txn._task_type == TaskType.DATABASE
    assert txn._db_type == "transaction"


# ── Тесты: кеш ───────────────────────────────────────


@pytest.mark.asyncio
async def test_cache_hit() -> None:
    """Кеш возвращает значение без вызова функции."""
    cache = FakeCache()
    cache.set("get_item:42", {"id": "42", "name": "test"})

    @db_method(cache_ttl=60, cache_key="get_item:{id}")
    async def get_item(self, id: str) -> dict:
        return {"id": id, "name": "new"}

    provider = FakeProvider(cache=cache)
    result = await get_item(provider, "42")

    assert result == {"id": "42", "name": "test"}


@pytest.mark.asyncio
async def test_cache_miss_stores_result() -> None:
    """При промахе кеша — результат записывается."""
    cache = FakeCache()

    @db_method(cache_ttl=30, cache_key="get_item:{id}")
    async def get_item(self, id: str) -> dict:
        return {"id": id, "name": "fresh"}

    provider = FakeProvider(cache=cache)
    result = await get_item(provider, "42")

    assert result == {"id": "42", "name": "fresh"}
    assert cache.get("get_item:42") == {"id": "42", "name": "fresh"}


@pytest.mark.asyncio
async def test_cache_disabled() -> None:
    """cache_ttl=0 — кеш не используется."""
    cache = FakeCache()

    @db_method(cache_ttl=0, cache_key="get_item:{id}")
    async def get_item(self, id: str) -> dict:
        return {"id": id}

    provider = FakeProvider(cache=cache)
    await get_item(provider, "42")

    assert len(cache._store) == 0


@pytest.mark.asyncio
async def test_cache_auto_key() -> None:
    """Автогенерация ключа когда cache_key не задан."""
    cache = FakeCache()

    @db_method(cache_ttl=30)
    async def count(self, table: str, filters: dict | None = None) -> int:
        return 42

    provider = FakeProvider(cache=cache)
    result = await count(provider, "users")

    assert result == 42
    assert len(cache._store) == 1
    key = list(cache._store.keys())[0]
    assert key.startswith("count:")


@pytest.mark.asyncio
async def test_cache_no_provider_instance() -> None:
    """Нет self._cache — кеш пропускается."""
    provider = FakeProvider(cache=None)

    @db_method(cache_ttl=30, cache_key="item:{id}")
    async def get_item(self, id: str) -> dict:
        return {"id": id}

    result = await get_item(provider, "42")
    assert result == {"id": "42"}


# ── Тесты: валидация ─────────────────────────────────


@pytest.mark.asyncio
async def test_validate_pydantic() -> None:
    """Валидация через Pydantic BaseModel."""
    from pydantic import BaseModel, ValidationError

    class ItemParams(BaseModel):
        id: str
        name: str

    @db_method(validate=ItemParams)
    async def create_item(self, id: str, name: str) -> dict:
        return {"id": id, "name": name}

    provider = FakeProvider()
    result = await create_item(provider, "1", "test")
    assert result == {"id": "1", "name": "test"}


@pytest.mark.asyncio
async def test_validate_pydantic_rejects() -> None:
    """Pydantic отклоняет невалидные данные."""
    from pydantic import BaseModel, ValidationError

    class StrictParams(BaseModel):
        id: int  # Требует int, получит str

    @db_method(validate=StrictParams)
    async def get_item(self, id: int) -> dict:
        return {"id": id}

    provider = FakeProvider()
    with pytest.raises(ValidationError):
        await get_item(provider, "not_an_int")


@pytest.mark.asyncio
async def test_validate_callable() -> None:
    """Валидация через callable."""
    validator = MagicMock()

    @db_method(validate=validator)
    async def get_item(self, id: str) -> dict:
        return {"id": id}

    provider = FakeProvider()
    await get_item(provider, "42")

    validator.assert_called_once_with({"id": "42"})


@pytest.mark.asyncio
async def test_validate_callable_error() -> None:
    """Callable-валидатор бросает ошибку."""
    def bad_validator(data: dict) -> None:
        raise ValueError("invalid data")

    @db_method(validate=bad_validator)
    async def get_item(self, id: str) -> dict:
        return {"id": id}

    provider = FakeProvider()
    with pytest.raises(ValueError, match="invalid data"):
        await get_item(provider, "42")


@pytest.mark.asyncio
async def test_validate_skips_self() -> None:
    """Self не попадает в данные валидации."""
    from pydantic import BaseModel

    class Params(BaseModel):
        id: str

    called_with = {}
    original_validate = Params.model_validate

    def capturing_validate(data, **kwargs):
        called_with.update(data)
        return original_validate(data, **kwargs)

    Params.model_validate = staticmethod(capturing_validate)

    @db_method(validate=Params)
    async def get_item(self, id: str) -> dict:
        return {"id": id}

    provider = FakeProvider()
    await get_item(provider, "42")

    assert "self" not in called_with
    assert called_with == {"id": "42"}

    Params.model_validate = original_validate


# ── Тесты: retry ──────────────────────────────────────


@pytest.mark.asyncio
async def test_retry_succeeds_on_second_attempt() -> None:
    """Функция падает один раз, потомucceeds."""
    call_count = 0

    @db_method(retry=2, retry_delay=0.01)
    async def flaky(self) -> str:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ConnectionError("timeout")
        return "ok"

    provider = FakeProvider()
    result = await flaky(provider)

    assert result == "ok"
    assert call_count == 2


@pytest.mark.asyncio
async def test_retry_exhausted() -> None:
    """Все попытки исчерпаны — выбрасывается последняя ошибка."""
    @db_method(retry=1, retry_delay=0.01)
    async def always_fail(self) -> str:
        raise RuntimeError("persistent failure")

    provider = FakeProvider()
    with pytest.raises(RuntimeError, match="persistent failure"):
        await always_fail(provider)


@pytest.mark.asyncio
async def test_retry_zero_no_retry() -> None:
    """retry=0 — повторных попыток нет."""
    call_count = 0

    @db_method(retry=0)
    async def fail_once(self) -> str:
        nonlocal call_count
        call_count += 1
        raise ValueError("boom")

    provider = FakeProvider()
    with pytest.raises(ValueError):
        await fail_once(provider)

    assert call_count == 1


@pytest.mark.asyncio
async def test_retry_preserves_exception_type() -> None:
    """Retry сохраняет тип исключения."""
    @db_method(retry=1, retry_delay=0.01)
    async def fail(self) -> str:
        raise KeyError("missing_key")

    provider = FakeProvider()
    with pytest.raises(KeyError):
        await fail(provider)


# ── Тесты: _resolve_cache_key ────────────────────────


def test_resolve_cache_key_with_template() -> None:
    key = _resolve_cache_key("get", "item:{id}", {"id": "42"})
    assert key == "item:42"


def test_resolve_cache_key_auto_generated() -> None:
    key = _resolve_cache_key("count", None, {"table": "users", "filters": None})
    assert key.startswith("count:")
    assert "table=users" in key


def test_resolve_cache_key_excludes_self() -> None:
    key = _resolve_cache_key("get", "item:{id}", {"self": "provider", "id": "42"})
    assert "self" not in key
    assert key == "item:42"


def test_resolve_cache_key_multiple_params() -> None:
    key = _resolve_cache_key(
        "search", "{table}:{field}", {"table": "users", "field": "email"},
    )
    assert key == "users:email"
