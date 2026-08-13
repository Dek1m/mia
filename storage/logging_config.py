"""Настройка логирования для MIA.

Формат по стандарту Argenta Team:
    [ISO8601-UTC] [LEVEL] [service] message {"key": "value", ...}

Env-переменные:
    SERVICE_NAME — имя сервиса (дефолт: "mia")
    LOG_LEVEL    — уровень логирования (дефолт: "INFO")
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

SERVICE_NAME = os.environ.get("SERVICE_NAME", "mia").lower()
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()


class ArgentaFormatter(logging.Formatter):
    """Форматтер по стандарту Argenta Team.

    Формат: [ISO8601-UTC] [LEVEL] [service] message {"key": "value"}
    """

    def __init__(self, service: str = "mia") -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        level = record.levelname
        dt = datetime.fromtimestamp(record.created, tz=timezone.utc)
        timestamp = dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond:06d}Z"
        message = record.getMessage()

        # Собираем JSON из extra-атрибутов (кроме стандартных logging)
        standard_attrs = {
            "name", "msg", "args", "created", "relativeCreated",
            "exc_info", "exc_text", "stack_info", "lineno", "funcName",
            "pathname", "filename", "module", "levelname", "levelno",
            "msecs", "message", "thread", "threadName", "process",
            "processName", "taskName", "pathname", "filename",
        }
        meta = {}
        for key, value in record.__dict__.items():
            if key not in standard_attrs and not key.startswith("_"):
                meta[key] = value

        meta_str = ""
        if meta:
            meta_str = " " + json.dumps(meta, default=str, ensure_ascii=False)

        return f"[{timestamp}] [{level}] [{self._service}] {message}{meta_str}"


def setup_logging(
    service: str | None = None,
    level: str | None = None,
    fmt: str = "posix",
) -> None:
    """Настроить логирование для сервиса.

    Использует argenta_logging для форматирования.
    Вызывать при старте приложения.

    Args:
        service: Имя сервиса. Если None — берётся из SERVICE_NAME.
        level: Уровень логирования (DEBUG, INFO, WARN, ERROR).
               Если None — берётся из LOG_LEVEL (дефолт INFO).
        fmt: Формат ('posix' или 'json').
    """
    svc = service or SERVICE_NAME
    log_level_str = (level or LOG_LEVEL).upper()
    log_level = getattr(logging, log_level_str, logging.INFO)

    try:
        from argenta_logging import setup_logging as argenta_setup

        argenta_setup(service=svc, level=level or log_level_str, fmt=fmt)
    except ImportError:
        # Fallback если argenta_logging не установлен
        handler = logging.StreamHandler()
        handler.setLevel(log_level)
        formatter = ArgentaFormatter(service=svc)
        handler.setFormatter(formatter)

        root = logging.getLogger()
        root.setLevel(log_level)
        root.addHandler(handler)

    logging.getLogger(__name__).info(
        "Logging configured",
        extra={"service": svc, "level": log_level_str, "format": fmt},
    )


def get_logger(name: str) -> logging.Logger:
    """Получить логгер. Обёртка для совместимости."""
    return logging.getLogger(name)
