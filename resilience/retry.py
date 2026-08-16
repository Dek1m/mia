"""Retry декоратор с exponential backoff."""
from __future__ import annotations

import time
import functools
from typing import Any, Callable

from argenta_logging import get_logger

log = get_logger(__name__)


def retry(
    max_attempts: int | None = None,
    base_delay: float | None = None,
    max_delay: float | None = None,
    on_retry: Callable[[int, Exception], None] | None = None,
) -> Callable:
    """Декоратор повторных попыток с exponential backoff.

    Args:
        max_attempts: Максимальное количество попыток (None → config).
        base_delay: Базовая задержка в секундах (None → config).
        max_delay: Максимальная задержка (None → config).
        on_retry: Callback при повторной попытке (attempt, error).
    """
    from core.config import MiaConfig
    cfg = MiaConfig.get()
    if max_attempts is None:
        max_attempts = cfg.get_value("resilience.retry.max_attempts", 3)
    if base_delay is None:
        base_delay = cfg.get_value("resilience.retry.base_delay", 0.5)
    if max_delay is None:
        max_delay = cfg.get_value("resilience.retry.max_delay", 30.0)

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_error: Exception | None = None

            for attempt in range(max_attempts):
                try:
                    return fn(*args, **kwargs)
                except Exception as e:
                    last_error = e

                    if attempt < max_attempts - 1:
                        delay = min(base_delay * (2**attempt), max_delay)
                        log.warning(
                            "Retry attempt",
                            extra={
                                "function": fn.__name__,
                                "attempt": attempt + 1,
                                "max_attempts": max_attempts,
                                "delay": delay,
                                "error": str(e),
                            },
                        )
                        if on_retry:
                            on_retry(attempt + 1, e)
                        time.sleep(delay)

            raise last_error  # type: ignore[misc]

        return wrapper

    return decorator
