"""RetryPolicy — класс для повторных попыток."""
from __future__ import annotations

import time
from typing import Callable, Any
from argenta_logging import get_logger

log = get_logger(__name__)


class RetryPolicy:
    """Политика повторных попыток с exponential backoff.
    
    Args:
        max_attempts: Максимальное количество попыток.
        base_delay: Базовая задержка (сек).
        max_delay: Максимальная задержка (сек).
    """
    
    def __init__(
        self,
        max_attempts: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
    ) -> None:
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._max_delay = max_delay
    
    def execute(self, fn: Callable, *args: Any, **kwargs: Any) -> Any:
        """Выполнить функцию с повторными попытками."""
        last_exception: Exception | None = None
        
        for attempt in range(self._max_attempts):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                last_exception = e
                if attempt < self._max_attempts - 1:
                    delay = min(self._base_delay * (2 ** attempt), self._max_delay)
                    log.warning("Retry attempt", extra={
                        "function": fn.__name__,
                        "attempt": attempt + 1,
                        "delay": delay,
                        "error": str(e),
                    })
                    time.sleep(delay)
        
        raise last_exception  # type: ignore[misc]
