"""Connection Pool — обёртка над asyncpg с sync-интерфейсом.

Пул соединений с PostgreSQL. Все методы sync для интеграции
с sync-кодом mia. Async/sync bridge через фоновый event loop.
"""
from __future__ import annotations

import asyncio
import time
import threading
from typing import Any

from argenta_logging import get_logger
from monitoring.metrics import (
    db_pool_connections_total,
    db_pool_queries_total,
    db_pool_query_duration_seconds,
    db_pool_errors_total,
)

log = get_logger(__name__)

# Маппинг SSL-режимов → asyncpg ssl-параметр
_SSL_MODE_MAP: dict[str, bool | None] = {
    "disable": False,
    "allow": None,
    "prefer": None,
    "require": True,
    "verify-ca": True,
    "verify-full": True,
}

# Транзиентные ошибки PostgreSQL для retry
_TRANSIENT_ERRORS: tuple[type[Exception], ...] = (
    ConnectionError,
    TimeoutError,
    OSError,
)


class ConnectionPool:
    """Пул соединений с PostgreSQL.

    Предоставляет sync-интерфейс для работы с asyncpg.
    Async/sync bridge реализован через фоновый event loop.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 5432,
        database: str = "mia",
        user: str = "mia",
        password: str = "",
        min_size: int = 2,
        max_size: int = 10,
        timeout: int = 30,
        ssl: str = "prefer",
        max_retries: int = 3,
        retry_base_delay: float = 0.5,
    ) -> None:
        self._host = host
        self._port = port
        self._database = database
        self._user = user
        self._password = password
        self._min_size = min_size
        self._max_size = max_size
        self._timeout = timeout
        self._ssl = ssl
        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay

        self._pool: Any | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._connected = False

    @property
    def is_connected(self) -> bool:
        """Пул подключён."""
        return self._connected

    def connect(self) -> None:
        """Инициализация пула соединений."""
        if self._connected:
            return

        # Фоновый event loop для asyncpg
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever,
            daemon=True,
            name="db-pool-loop",
        )
        self._thread.start()

        # Создание пула через async
        future = asyncio.run_coroutine_threadsafe(
            self._async_connect(),
            self._loop,
        )
        self._pool = future.result(timeout=self._timeout)
        self._connected = True

        # Метрики
        db_pool_connections_total.labels(state="idle").inc(self._min_size)

        log.info(
            "Connection pool connected",
            extra={
                "host": self._host,
                "port": self._port,
                "database": self._database,
            },
        )

    async def _async_connect(self) -> Any:
        """Асинхронное подключение к БД."""
        import asyncpg

        ssl_mode = _SSL_MODE_MAP.get(self._ssl, None)

        return await asyncpg.create_pool(
            host=self._host,
            port=self._port,
            database=self._database,
            user=self._user,
            password=self._password,
            min_size=self._min_size,
            max_size=self._max_size,
            timeout=self._timeout,
            ssl=ssl_mode,
        )

    def close(self) -> None:
        """Закрытие пула соединений."""
        if not self._connected:
            return

        # Закрытие пула через async
        future = asyncio.run_coroutine_threadsafe(
            self._async_close(),
            self._loop,
        )
        try:
            future.result(timeout=5)
        except Exception:
            pass

        # Остановка event loop
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=2)

        self._connected = False
        self._pool = None

        # Сброс метрик
        db_pool_connections_total.labels(state="idle").dec(self._min_size)
        log.info("Connection pool closed")

    async def _async_close(self) -> None:
        """Асинхронное закрытие пула."""
        if self._pool:
            await self._pool.close()

    # ──────────────────────────────────────────────
    # Sync-обёртки над asyncpg
    # ──────────────────────────────────────────────

    def fetchrow(self, query: str, *args: Any) -> dict | None:
        """Получить одну строку."""
        return self._run_with_retry(self._async_fetchrow, query, *args)

    async def _async_fetchrow(self, query: str, *args: Any) -> dict | None:
        async with self._pool.acquire() as conn:
            return await conn.fetchrow(query, *args)

    def fetch(self, query: str, *args: Any) -> list[dict]:
        """Получить список строк."""
        return self._run_with_retry(self._async_fetch, query, *args)

    async def _async_fetch(self, query: str, *args: Any) -> list[dict]:
        async with self._pool.acquire() as conn:
            return await conn.fetch(query, *args)

    def fetchval(self, query: str, *args: Any) -> Any:
        """Получить одно значение."""
        return self._run_with_retry(self._async_fetchval, query, *args)

    async def _async_fetchval(self, query: str, *args: Any) -> Any:
        async with self._pool.acquire() as conn:
            return await conn.fetchval(query, *args)

    def execute(self, query: str, *args: Any) -> str:
        """Выполнить команду."""
        return self._run_with_retry(self._async_execute, query, *args)

    async def _async_execute(self, query: str, *args: Any) -> str:
        async with self._pool.acquire() as conn:
            return await conn.execute(query, *args)

    # ──────────────────────────────────────────────
    # Транзакции
    # ──────────────────────────────────────────────

    def transaction(self) -> TransactionContext:
        """Контекстный менеджер транзакции."""
        return TransactionContext(self)

    # ──────────────────────────────────────────────
    # Health check
    # ──────────────────────────────────────────────

    def health_check(self) -> bool:
        """Проверка здоровья соединения."""
        try:
            result = self.fetchval("SELECT 1")
            return result == 1
        except Exception as e:
            db_pool_errors_total.labels(error_type="health_check").inc()
            log.error("Health check failed", extra={"error": str(e)})
            return False

    # ──────────────────────────────────────────────
    # Выполнение с retry
    # ──────────────────────────────────────────────

    def _run_with_retry(
        self,
        coro_factory: Any,
        *args: Any,
    ) -> Any:
        """Запуск coroutine с retry для транзиентных ошибок."""
        last_error: Exception | None = None

        for attempt in range(self._max_retries):
            try:
                return self._run(coro_factory(*args))
            except _TRANSIENT_ERRORS as e:
                last_error = e
                if attempt < self._max_retries - 1:
                    delay = min(
                        self._retry_base_delay * (2**attempt),
                        30.0,
                    )
                    db_pool_errors_total.labels(error_type="transient").inc()
                    log.warning(
                        "Transient DB error, retrying",
                        extra={
                            "attempt": attempt + 1,
                            "max_attempts": self._max_retries,
                            "delay": delay,
                            "error": str(e),
                        },
                    )
                    time.sleep(delay)

        raise last_error  # type: ignore[misc]

    def _run(self, coro: Any) -> Any:
        """Запуск coroutine в фоновом loop."""
        if not self._connected:
            raise RuntimeError("Pool not connected. Call connect() first.")

        # Определяем операцию для метрик
        op_name = coro.cr_frame.f_code.co_name.replace("_async_", "") if hasattr(coro, "cr_frame") else "unknown"

        start = time.monotonic()
        try:
            future = asyncio.run_coroutine_threadsafe(coro, self._loop)
            result = future.result(timeout=self._timeout)
            duration = time.monotonic() - start

            # Метрики
            db_pool_queries_total.labels(operation=op_name).inc()
            db_pool_query_duration_seconds.labels(operation=op_name).observe(duration)

            if duration > 0.1:
                log.warning(
                    "Slow DB query",
                    extra={"operation": op_name, "duration": duration},
                )

            return result
        except Exception as e:
            duration = time.monotonic() - start
            db_pool_errors_total.labels(error_type=type(e).__name__).inc()
            db_pool_query_duration_seconds.labels(operation=op_name).observe(duration)
            raise


class TransactionContext:
    """Контекстный менеджер для транзакций.

    Предоставляет sync-интерфейс для выполнения запросов
    внутри одной транзакции.
    """

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool
        self._conn: Any | None = None
        self._tx: Any | None = None

    def __enter__(self) -> TransactionContext:
        if not self._pool._connected:
            raise RuntimeError("Pool not connected. Call connect() first.")

        # Асинхронное получение соединения и начала транзакции
        future = asyncio.run_coroutine_threadsafe(
            self._async_start(),
            self._pool._loop,
        )
        future.result(timeout=self._pool._timeout)
        return self

    async def _async_start(self) -> None:
        """Асинхронный старт транзакции."""
        self._conn = await self._pool._pool.acquire()
        self._tx = self._conn.transaction()
        await self._tx.start()

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        try:
            if exc_type:
                # Rollback при ошибке
                future = asyncio.run_coroutine_threadsafe(
                    self._async_rollback(),
                    self._pool._loop,
                )
                future.result(timeout=5)
            else:
                # Commit при успехе
                future = asyncio.run_coroutine_threadsafe(
                    self._async_commit(),
                    self._pool._loop,
                )
                future.result(timeout=self._pool._timeout)
        finally:
            # Возврат соединения в пул
            if self._conn:
                release_future = asyncio.run_coroutine_threadsafe(
                    self._pool._pool.release(self._conn),
                    self._pool._loop,
                )
                try:
                    release_future.result(timeout=5)
                except Exception:
                    pass

    async def _async_commit(self) -> None:
        """Асинхронный commit."""
        if self._tx:
            await self._tx.commit()

    async def _async_rollback(self) -> None:
        """Асинхронный rollback."""
        if self._tx:
            await self._tx.rollback()

    def fetchrow(self, query: str, *args: Any) -> dict | None:
        """Получить одну строку внутри транзакции."""
        return self._run(self._async_fetchrow(query, *args))

    async def _async_fetchrow(self, query: str, *args: Any) -> dict | None:
        return await self._conn.fetchrow(query, *args)

    def fetch(self, query: str, *args: Any) -> list[dict]:
        """Получить список строк внутри транзакции."""
        return self._run(self._async_fetch(query, *args))

    async def _async_fetch(self, query: str, *args: Any) -> list[dict]:
        return await self._conn.fetch(query, *args)

    def fetchval(self, query: str, *args: Any) -> Any:
        """Получить одно значение внутри транзакции."""
        return self._run(self._async_fetchval(query, *args))

    async def _async_fetchval(self, query: str, *args: Any) -> Any:
        return await self._conn.fetchval(query, *args)

    def execute(self, query: str, *args: Any) -> str:
        """Выполнить команду внутри транзакции."""
        return self._run(self._async_execute(query, *args))

    async def _async_execute(self, query: str, *args: Any) -> str:
        return await self._conn.execute(query, *args)

    def _run(self, coro: Any) -> Any:
        """Запуск coroutine в фоновом loop."""
        future = asyncio.run_coroutine_threadsafe(coro, self._pool._loop)
        return future.result(timeout=self._pool._timeout)
