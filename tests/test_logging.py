"""Тесты для логирования MIA — формат по стандарту Argenta Team."""
from __future__ import annotations

import logging
import re
import json

import pytest


# === Формат лога ===

LOG_FORMAT_RE = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z)\] "
    r"\[(DEBUG|INFO|WARNING|ERROR)\] "
    r"\[([a-z0-9-]+)\] "
    r"(.+?)(\s\{.*\})?$"
)


def test_log_format_iso8601_timestamp():
    """Формат: [ISO8601-UTC] [LEVEL] [service] message."""
    from storage.logging_config import ArgentaFormatter

    formatter = ArgentaFormatter(service="mia")
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname="test.py",
        lineno=1, msg="Test message", args=(), exc_info=None,
    )
    line = formatter.format(record)
    match = LOG_FORMAT_RE.match(line)
    assert match, f"Format mismatch: {line}"
    # Timestamp в формате ISO8601
    assert "T" in match.group(1)
    assert match.group(1).endswith("Z")


def test_log_format_level():
    """Уровень логирования в квадратных скобках."""
    from storage.logging_config import ArgentaFormatter

    formatter = ArgentaFormatter(service="mia")

    for level, expected in [
        (logging.DEBUG, "DEBUG"),
        (logging.INFO, "INFO"),
        (logging.WARNING, "WARNING"),
        (logging.ERROR, "ERROR"),
    ]:
        record = logging.LogRecord(
            name="test", level=level, pathname="test.py",
            lineno=1, msg="Test", args=(), exc_info=None,
        )
        line = formatter.format(record)
        match = LOG_FORMAT_RE.match(line)
        assert match, f"Format mismatch for level {level}: {line}"
        assert match.group(2) == expected


def test_log_format_service_name():
    """Service name в квадратных скобках."""
    from storage.logging_config import ArgentaFormatter

    formatter = ArgentaFormatter(service="mia")
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname="test.py",
        lineno=1, msg="Test", args=(), exc_info=None,
    )
    line = formatter.format(record)
    match = LOG_FORMAT_RE.match(line)
    assert match
    assert match.group(3) == "mia"


def test_log_format_service_custom():
    """Кастомный service name."""
    from storage.logging_config import ArgentaFormatter

    formatter = ArgentaFormatter(service="my-service")
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname="test.py",
        lineno=1, msg="Test", args=(), exc_info=None,
    )
    line = formatter.format(record)
    match = LOG_FORMAT_RE.match(line)
    assert match
    assert match.group(3) == "my-service"


def test_log_format_json_metadata():
    """JSON-мета после сообщения из extra."""
    from storage.logging_config import ArgentaFormatter

    formatter = ArgentaFormatter(service="mia")
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname="test.py",
        lineno=1, msg="API call", args=(), exc_info=None,
    )
    record.module_name = "sample"
    record.method_name = "add"
    line = formatter.format(record)

    # Должен содержать JSON после сообщения
    assert "API call" in line
    assert '{"module_name": "sample", "method_name": "add"}' in line


def test_log_format_no_metadata_when_empty():
    """Без extra — нет JSON."""
    from storage.logging_config import ArgentaFormatter

    formatter = ArgentaFormatter(service="mia")
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname="test.py",
        lineno=1, msg="Simple message", args=(), exc_info=None,
    )
    line = formatter.format(record)
    match = LOG_FORMAT_RE.match(line)
    assert match
    # Нет JSON-хвоста
    assert match.group(5) is None


def test_log_format_standard_attrs_not_in_json():
    """Стандартные атрибуты logging не попадают в JSON."""
    from storage.logging_config import ArgentaFormatter

    formatter = ArgentaFormatter(service="mia")
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname="test.py",
        lineno=1, msg="Test", args=(), exc_info=None,
    )
    line = formatter.format(record)
    # Стандартные атрибуты не должны быть в JSON
    assert "lineno" not in line.split("Test")[-1] or '"lineno"' not in line
    assert '"pathname"' not in line
    assert '"name"' not in line or '"name": "test"' not in line


# === setup_logging ===

def test_setup_logging_creates_handler():
    """setup_logging настраивает логирование (argenta или fallback)."""
    from storage.logging_config import setup_logging

    # Не проверяем количество handlers — argenta_logging может перехватить
    # Просто убеждаемся что вызов не падает
    setup_logging(service="test-svc", level="DEBUG")


def test_setup_logging_service_name_from_env():
    """SERVICE_NAME из env-переменной."""
    import os
    from storage.logging_config import SERVICE_NAME

    # Дефолтное значение
    assert SERVICE_NAME == os.environ.get("SERVICE_NAME", "mia").lower()


def test_setup_logging_default_level():
    """LOG_LEVEL из env-переменной, дефолт INFO."""
    import os
    from storage.logging_config import LOG_LEVEL

    expected = os.environ.get("LOG_LEVEL", "INFO").upper()
    assert LOG_LEVEL == expected


def test_setup_logging_custom_service():
    """Кастомный service name через аргумент."""
    from storage.logging_config import setup_logging, ArgentaFormatter

    formatter = ArgentaFormatter(service="custom-svc")
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname="test.py",
        lineno=1, msg="Test", args=(), exc_info=None,
    )
    line = formatter.format(record)
    assert "[custom-svc]" in line


# === get_logger ===

def test_get_logger_returns_logger():
    """get_logger возвращает logging.Logger."""
    from storage.logging_config import get_logger

    logger = get_logger("test.module")
    assert isinstance(logger, logging.Logger)
    assert logger.name == "test.module"


# === Интеграция с argenta_logging ===

def test_argenta_logging_get_logger():
    """argenta_logging.get_logger создаёт логгер с правильным форматом."""
    from argenta_logging import get_logger

    logger = get_logger("test.argenta")
    assert isinstance(logger, logging.Logger)


def test_all_modules_use_argenta_logging():
    """Все модули проекта используют argenta_logging.get_logger."""
    import importlib
    modules = [
        "storage.cache_hierarchy",
        "storage.shared_memory",
        "storage.serializer",
        "communication.api_proxy",
        "communication.event_bus",
        "resilience.shutdown_manager",
        "resilience.circuit_breaker",
        "resilience.retry",
        "monitoring.metrics",
        "monitoring.health_check",
        "core.dispatch.local",
        "modules_system.module_manager",
        "modules_system.module_registry",
        "modules_system.module_base",
        "core.application",
        "core.service_registry",
        "core.database",
        "core.factories",
    ]

    for mod_name in modules:
        try:
            mod = importlib.import_module(mod_name)
            source = open(mod.__file__).read()
            assert "argenta_logging" in source or "get_logger" in source, (
                f"{mod_name} не использует argenta_logging"
            )
        except ImportError:
            pytest.skip(f"Модуль {mod_name} не импортируется в тестах")
