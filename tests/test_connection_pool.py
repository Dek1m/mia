"""Тесты для ConnectionPool."""
from __future__ import annotations

import pytest
from unittest.mock import Mock, patch, MagicMock
from storage.connection_pool import ConnectionPool, TransactionContext


def test_pool_creation():
    """Pool создаётся с параметрами."""
    pool = ConnectionPool(
        host="localhost",
        port=5432,
        database="test",
        user="test",
        password="secret",
    )
    assert pool._host == "localhost"
    assert pool._port == 5432
    assert pool._database == "test"
    assert pool._user == "test"
    assert pool._password == "secret"


def test_pool_default_params():
    """Pool принимает дефолтные параметры."""
    pool = ConnectionPool()
    assert pool._host == "localhost"
    assert pool._port == 5432
    assert pool._database == "mia"
    assert pool._min_size == 2
    assert pool._max_size == 10
    assert pool._timeout == 30
    assert pool._ssl == "prefer"
    assert pool._max_retries == 3
    assert pool._retry_base_delay == 0.5


def test_pool_not_connected_by_default():
    """Pool не подключён по умолчанию."""
    pool = ConnectionPool()
    assert pool.is_connected is False


def test_pool_not_connected_error():
    """Ошибка при вызове методов без подключения."""
    pool = ConnectionPool()
    with pytest.raises(RuntimeError, match="Pool not connected"):
        pool.fetchrow("SELECT 1")


def test_pool_fetchrow_not_connected():
    """fetchrow выбрасывает ошибку без подключения."""
    pool = ConnectionPool()
    with pytest.raises(RuntimeError, match="Pool not connected"):
        pool.fetchrow("SELECT 1")


def test_pool_fetch_not_connected():
    """fetch выбрасывает ошибку без подключения."""
    pool = ConnectionPool()
    with pytest.raises(RuntimeError, match="Pool not connected"):
        pool.fetch("SELECT 1")


def test_pool_fetchval_not_connected():
    """fetchval выбрасывает ошибку без подключения."""
    pool = ConnectionPool()
    with pytest.raises(RuntimeError, match="Pool not connected"):
        pool.fetchval("SELECT 1")


def test_pool_execute_not_connected():
    """execute выбрасывает ошибку без подключения."""
    pool = ConnectionPool()
    with pytest.raises(RuntimeError, match="Pool not connected"):
        pool.execute("SELECT 1")


def test_pool_transaction_not_connected():
    """transaction выбрасывает ошибку без подключения."""
    pool = ConnectionPool()
    with pytest.raises(RuntimeError, match="Pool not connected"):
        with pool.transaction():
            pass


@patch("storage.connection_pool.asyncpg")
def test_pool_connect(mock_asyncpg):
    """Pool подключается к БД."""
    mock_pool = MagicMock()
    mock_asyncpg.create_pool.return_value = mock_pool

    pool = ConnectionPool()
    pool.connect()

    assert pool.is_connected is True
    assert pool._pool is mock_pool

    # Проверяем параметры вызова
    mock_asyncpg.create_pool.assert_called_once_with(
        host="localhost",
        port=5432,
        database="mia",
        user="mia",
        password="",
        min_size=2,
        max_size=10,
        timeout=30,
        ssl=None,  # "prefer" → None в маппинге
    )


@patch("storage.connection_pool.asyncpg")
def test_pool_connect_custom_params(mock_asyncpg):
    """Pool подключается с кастомными параметрами."""
    mock_pool = MagicMock()
    mock_asyncpg.create_pool.return_value = mock_pool

    pool = ConnectionPool(
        host="db.example.com",
        port=5433,
        database="mydb",
        user="admin",
        password="secret",
        min_size=5,
        max_size=20,
        timeout=60,
        ssl="require",
    )
    pool.connect()

    mock_asyncpg.create_pool.assert_called_once_with(
        host="db.example.com",
        port=5433,
        database="mydb",
        user="admin",
        password="secret",
        min_size=5,
        max_size=20,
        timeout=60,
        ssl=True,  # "require" → True
    )


@patch("storage.connection_pool.asyncpg")
def test_pool_connect_idempotent(mock_asyncpg):
    """Повторный connect() не создаёт новый пул."""
    mock_pool = MagicMock()
    mock_asyncpg.create_pool.return_value = mock_pool

    pool = ConnectionPool()
    pool.connect()
    pool.connect()  # Второй вызов

    # create_pool вызывается только один раз
    assert mock_asyncpg.create_pool.call_count == 1


@patch("storage.connection_pool.asyncpg")
def test_pool_close(mock_asyncpg):
    """Pool закрывается корректно."""
    mock_pool = MagicMock()
    mock_asyncpg.create_pool.return_value = mock_pool

    pool = ConnectionPool()
    pool.connect()
    pool.close()

    assert pool.is_connected is False
    assert pool._pool is None


@patch("storage.connection_pool.asyncpg")
def test_pool_close_idempotent(mock_asyncpg):
    """Повторный close() не вызывает ошибку."""
    mock_pool = MagicMock()
    mock_asyncpg.create_pool.return_value = mock_pool

    pool = ConnectionPool()
    pool.connect()
    pool.close()
    pool.close()  # Второй вызов

    assert pool.is_connected is False


@patch("storage.connection_pool.asyncpg")
def test_pool_health_check_success(mock_asyncpg):
    """Health check возвращает True при успешном запросе."""
    mock_pool = MagicMock()
    mock_asyncpg.create_pool.return_value = mock_pool

    pool = ConnectionPool()
    pool.connect()

    # Мокаем fetchval для возврата 1
    with patch.object(pool, "fetchval", return_value=1):
        assert pool.health_check() is True


@patch("storage.connection_pool.asyncpg")
def test_pool_health_check_failure(mock_asyncpg):
    """Health check возвращает False при ошибке."""
    mock_pool = MagicMock()
    mock_asyncpg.create_pool.return_value = mock_pool

    pool = ConnectionPool()
    pool.connect()

    # Мокаем fetchval для выброса исключения
    with patch.object(pool, "fetchval", side_effect=Exception("Connection lost")):
        assert pool.health_check() is False


@patch("storage.connection_pool.asyncpg")
def test_ssl_mode_mapping(mock_asyncpg):
    """SSL-режимы маппятся correctamente."""
    mock_pool = MagicMock()
    mock_asyncpg.create_pool.return_value = mock_pool

    test_cases = [
        ("disable", False),
        ("allow", None),
        ("prefer", None),
        ("require", True),
        ("verify-ca", True),
        ("verify-full", True),
    ]

    for ssl_mode, expected_ssl in test_cases:
        mock_asyncpg.reset_mock()
        pool = ConnectionPool(ssl=ssl_mode)
        pool.connect()

        call_kwargs = mock_asyncpg.create_pool.call_args[1]
        assert call_kwargs["ssl"] == expected_ssl, f"ssl_mode={ssl_mode}"

        pool.close()


def test_transaction_context_creation():
    """TransactionContext создаётся."""
    pool = ConnectionPool()
    ctx = TransactionContext(pool)
    assert ctx._pool is pool
    assert ctx._conn is None
    assert ctx._tx is None
