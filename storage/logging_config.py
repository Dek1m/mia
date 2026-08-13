"""Настройка логирования для MIA."""
from __future__ import annotations

import logging
import os
from typing import Any

SERVICE_NAME = os.environ.get("SERVICE_NAME", "mia").lower()


def setup_logging(
    service: str | None = None,
    level: str = "INFO",
    fmt: str = "posix",
) -> None:
    """Настроить логирование для сервиса.

    Использует argenta_logging для форматирования.
    Вызывать при старте приложения.

    Args:
        service: Имя сервиса. Если None — берётся из SERVICE_NAME.
        level: Уровень логирования (DEBUG, INFO, WARN, ERROR).
        fmt: Формат ('posix' или 'json').
    """
    svc = service or SERVICE_NAME
    log_level = getattr(logging, level.upper(), logging.INFO)

    try:
        from argenta_logging import setup_logging as argenta_setup

        argenta_setup(service=svc, level=level, fmt=fmt)
    except ImportError:
        # Fallback если argenta_logging не установлен
        handler = logging.StreamHandler()
        handler.setLevel(log_level)
        formatter = logging.Formatter(
            fmt=f"[%(asctime)s] [%(levelname)s] [{svc}] %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S.%fZ",
        )
        handler.setFormatter(formatter)

        root = logging.getLogger()
        root.setLevel(log_level)
        root.addHandler(handler)

    logging.getLogger(__name__).info(
        "Logging configured",
        extra={"service": svc, "level": level, "format": fmt},
    )
