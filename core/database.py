"""Database facade — делегирует CRUD провайдерам."""
from __future__ import annotations

import time
from typing import Any

from argenta_logging import get_logger
from core.interfaces import IDatabase
from monitoring.metrics import (
    database_cache_hits_total,
    database_cache_misses_total,
    database_operation_duration_seconds,
    database_operations_total,
)

log = get_logger(__name__)


class Database(IDatabase):
    """Фасад Database — реестр провайдеров.

    Если задан dispatcher — CRUD идёт через него (обычно LocalInvoke).
    Иначе — прямой _delegate в провайдер.
    """

    def __init__(
        self,
        cache: Any | None = None,
        dispatcher: Any | None = None,
        stats_writer: Any | None = None,
        module_meta: Any | None = None,
    ) -> None:
        self._providers: dict[str, Any] = {}
        self._default_provider: str | None = None
        self._cache = cache
        self._dispatcher = dispatcher
        self._stats_writer = stats_writer
        self._module_meta = module_meta

    def register_provider(self, name: str, provider: Any, is_default: bool = False) -> None:
        self._providers[name] = provider
        if is_default:
            self._default_provider = name

    def get_provider(self, name: str | None = None) -> Any:
        target = name or self._default_provider
        if target not in self._providers:
            raise KeyError(f"Provider '{target}' not registered")
        return self._providers[target]

    def _delegate(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        return getattr(self.get_provider(), method_name)(*args, **kwargs)

    def _call(self, stub: Any, operation: str, *args: Any) -> Any:
        start = time.monotonic()
        try:
            if self._dispatcher is not None:
                result = self._dispatcher.dispatch(stub, *args)
            else:
                result = stub(*args)
            database_operations_total.labels(operation=operation, status="ok").inc()
            return result
        except Exception as e:
            database_operations_total.labels(operation=operation, status="error").inc()
            log.error(f"db_{operation}_error", extra={"error": str(e)})
            raise
        finally:
            database_operation_duration_seconds.labels(operation=operation).observe(
                time.monotonic() - start
            )

    def get(self, table: str, id: str) -> dict | None:
        cache_key = f"db:{table}:{id}"
        cached = self._cache_get(cache_key, "get")
        if cached is not None:
            log.debug("cache_hit", extra={"table": table, "id": id})
            return cached
        result = self._call(self._provider_get, "get", table, id)
        log.debug("db_get", extra={"table": table, "id": id, "found": result is not None})
        if result is not None and self._cache is not None:
            self._cache.set(cache_key, result)
        return result

    def get_by_field(self, table: str, field: str, value: Any) -> dict | None:
        cache_key = f"db:{table}:{field}:{value}"
        cached = self._cache_get(cache_key, "get_by_field")
        if cached is not None:
            log.debug("cache_hit", extra={"table": table, "field": field})
            return cached
        result = self._call(self._provider_get_by_field, "get_by_field", table, field, value)
        log.debug("db_get_by_field", extra={"table": table, "field": field, "found": result is not None})
        if result is not None and self._cache is not None:
            self._cache.set(cache_key, result)
        return result

    def insert(self, table: str, data: dict) -> str:
        result = self._call(self._provider_insert, "insert", table, data)
        log.debug("db_insert", extra={"table": table, "id": result})
        if self._cache is not None:
            self._invalidate_table(table)
        return result

    def update(self, table: str, id: str, data: dict) -> dict | None:
        result = self._call(self._provider_update, "update", table, id, data)
        log.debug("db_update", extra={"table": table, "id": id})
        if self._cache is not None:
            self._cache.delete(f"db:{table}:{id}")
        return result

    def delete(self, table: str, id: str) -> bool:
        result = self._call(self._provider_delete, "delete", table, id)
        log.debug("db_delete", extra={"table": table, "id": id, "success": result})
        if result and self._cache is not None:
            self._cache.delete(f"db:{table}:{id}")
        return result

    def exists(self, table: str, id: str) -> bool:
        return self._delegate("exists", table, id)

    def count(self, table: str, filters: dict | None = None) -> int:
        return self._delegate("count", table, filters)

    def list(self, table: str, filters: dict | None = None, limit: int | None = None, offset: int = 0) -> list[dict]:
        if limit is None:
            from core.config import MiaConfig

            limit = MiaConfig.get().get_value("core.database.list_limit", 100)
        return self._delegate("list", table, filters, limit, offset)

    async def fetch(self, query: str, *params: Any) -> list[dict]:
        return await self._delegate("fetch", query, *params)

    async def execute(self, query: str, *params: Any) -> str:
        return await self._delegate("execute", query, *params)

    def _provider_get(self, table: str, id: str) -> dict | None:
        return self._delegate("get", table, id)

    def _provider_get_by_field(self, table: str, field: str, value: Any) -> dict | None:
        return self._delegate("get_by_field", table, field, value)

    def _provider_insert(self, table: str, data: dict) -> str:
        return self._delegate("insert", table, data)

    def _provider_update(self, table: str, id: str, data: dict) -> dict | None:
        return self._delegate("update", table, id, data)

    def _provider_delete(self, table: str, id: str) -> bool:
        return self._delegate("delete", table, id)

    def set_cache(self, cache: Any) -> None:
        self._cache = cache

    def set_dispatcher(self, dispatcher: Any) -> None:
        self._dispatcher = dispatcher

    def _cache_get(self, cache_key: str, method: str) -> Any | None:
        use_meta = bool(self._module_meta and method in getattr(self._module_meta, "cache_rules", {}))
        if not use_meta and self._cache is None:
            return None
        if self._cache is None:
            database_cache_misses_total.inc()
            return None
        cached = self._cache.get(cache_key)
        if cached is not None:
            database_cache_hits_total.labels(level="l0").inc()
            return cached
        database_cache_misses_total.inc()
        return None

    def _invalidate_table(self, table: str) -> None:
        if hasattr(self._cache, "clear"):
            pass

    def shutdown(self) -> None:
        """Освобождение ресурсов если нужно."""
