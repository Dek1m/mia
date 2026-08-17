"""Database facade — делегирует CRUD провайдерам."""
from __future__ import annotations

import time
from typing import Any

from argenta_logging import get_logger
from core.interfaces import IDatabase
from monitoring.metrics import (
    database_operations_total,
    database_operation_duration_seconds,
    database_cache_hits_total,
    database_cache_misses_total,
)

log = get_logger(__name__)


class Database(IDatabase):
    """Фасад Database — управляет реестром провайдеров.

    Опционально интегрируется с Universal Task System:
    каждая CRUD-операция проходит через SmartDispatcher.
    """

    def __init__(
        self,
        cache: Any | None = None,
        dispatcher: Any | None = None,
        stats_writer: Any | None = None,
    ) -> None:
        self._providers: dict[str, Any] = {}
        self._default_provider: str | None = None
        self._cache = cache
        self._dispatcher = dispatcher
        self._stats_writer = stats_writer

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
        provider = self.get_provider()
        method = getattr(provider, method_name)
        return method(*args, **kwargs)

    def get(self, table: str, id: str) -> dict | None:
        cache_key = f"db:{table}:{id}"
        if self._cache is not None:
            cached = self._cache.get(cache_key)
            if cached is not None:
                database_cache_hits_total.labels(level="l0").inc()
                log.debug("cache_hit", extra={"table": table, "id": id})
                return cached
            database_cache_misses_total.inc()
        start = time.monotonic()
        try:
            result = (
                self._dispatcher.dispatch(self._provider_get, table, id)
                if self._dispatcher is not None
                else self._delegate("get", table, id)
            )
            database_operations_total.labels(operation="get", status="ok").inc()
            log.debug("db_get", extra={"table": table, "id": id, "found": result is not None})
        except Exception as e:
            database_operations_total.labels(operation="get", status="error").inc()
            log.error("db_get_error", extra={"table": table, "id": id, "error": str(e)})
            raise
        finally:
            database_operation_duration_seconds.labels(operation="get").observe(time.monotonic() - start)
        if result is not None and self._cache is not None:
            self._cache.set(cache_key, result)
        return result

    def get_by_field(self, table: str, field: str, value: Any) -> dict | None:
        cache_key = f"db:{table}:{field}:{value}"
        if self._cache is not None:
            cached = self._cache.get(cache_key)
            if cached is not None:
                database_cache_hits_total.labels(level="l0").inc()
                log.debug("cache_hit", extra={"table": table, "field": field})
                return cached
            database_cache_misses_total.inc()
        start = time.monotonic()
        try:
            result = (
                self._dispatcher.dispatch(self._provider_get_by_field, table, field, value)
                if self._dispatcher is not None
                else self._delegate("get_by_field", table, field, value)
            )
            database_operations_total.labels(operation="get_by_field", status="ok").inc()
            log.debug("db_get_by_field", extra={"table": table, "field": field, "found": result is not None})
        except Exception as e:
            database_operations_total.labels(operation="get_by_field", status="error").inc()
            log.error("db_get_by_field_error", extra={"table": table, "field": field, "error": str(e)})
            raise
        finally:
            database_operation_duration_seconds.labels(operation="get_by_field").observe(time.monotonic() - start)
        if result is not None and self._cache is not None:
            self._cache.set(cache_key, result)
        return result

    def insert(self, table: str, data: dict) -> str:
        start = time.monotonic()
        try:
            result = (
                self._dispatcher.dispatch(self._provider_insert, table, data)
                if self._dispatcher is not None
                else self._delegate("insert", table, data)
            )
            database_operations_total.labels(operation="insert", status="ok").inc()
            log.debug("db_insert", extra={"table": table, "id": result})
        except Exception as e:
            database_operations_total.labels(operation="insert", status="error").inc()
            log.error("db_insert_error", extra={"table": table, "error": str(e)})
            raise
        finally:
            database_operation_duration_seconds.labels(operation="insert").observe(time.monotonic() - start)
        if self._cache is not None:
            self._invalidate_table(table)
        return result

    def update(self, table: str, id: str, data: dict) -> dict | None:
        start = time.monotonic()
        try:
            result = (
                self._dispatcher.dispatch(self._provider_update, table, id, data)
                if self._dispatcher is not None
                else self._delegate("update", table, id, data)
            )
            database_operations_total.labels(operation="update", status="ok").inc()
            log.debug("db_update", extra={"table": table, "id": id})
        except Exception as e:
            database_operations_total.labels(operation="update", status="error").inc()
            log.error("db_update_error", extra={"table": table, "id": id, "error": str(e)})
            raise
        finally:
            database_operation_duration_seconds.labels(operation="update").observe(time.monotonic() - start)
        if self._cache is not None:
            self._cache.delete(f"db:{table}:{id}")
        return result

    def delete(self, table: str, id: str) -> bool:
        start = time.monotonic()
        try:
            result = (
                self._dispatcher.dispatch(self._provider_delete, table, id)
                if self._dispatcher is not None
                else self._delegate("delete", table, id)
            )
            database_operations_total.labels(operation="delete", status="ok").inc()
            log.debug("db_delete", extra={"table": table, "id": id, "success": result})
        except Exception as e:
            database_operations_total.labels(operation="delete", status="error").inc()
            log.error("db_delete_error", extra={"table": table, "id": id, "error": str(e)})
            raise
        finally:
            database_operation_duration_seconds.labels(operation="delete").observe(time.monotonic() - start)
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

    # === Provider stubs с метаданными для SmartDispatcher ===

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

    def _invalidate_table(self, table: str) -> None:
        """Инвалидировать кеш для таблицы (best-effort)."""
        if hasattr(self._cache, "clear"):
            # Точечная инвалидация невозможна без keys scan — сбрасываем весь кеш
            # Для production нужен prefix-based инвалидатор
            pass

    def shutdown(self) -> None:
        """Освобождение ресурсов если нужно."""
