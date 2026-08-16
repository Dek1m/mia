"""Circuit Breaker — защита от каскадных сбоев."""
from __future__ import annotations

import threading
import time
from enum import Enum
from typing import Any, Callable

from argenta_logging import get_logger

log = get_logger(__name__)


class State(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Circuit Breaker с тремя состояниями.

    Args:
        failure_threshold: Количество ошибок для перехода в OPEN.
        recovery_timeout: Секунд до попытки перехода в HALF_OPEN.
        success_threshold: Количество успехов для возврата в CLOSED.
    """

    def __init__(
        self,
        failure_threshold: int | None = None,
        recovery_timeout: float | None = None,
        success_threshold: int | None = None,
    ) -> None:
        from core.config import MiaConfig
        cfg = MiaConfig.get()
        self._failure_threshold = failure_threshold if failure_threshold is not None else cfg.get_value("resilience.circuit_breaker.failure_threshold", 5)
        self._recovery_timeout = recovery_timeout if recovery_timeout is not None else cfg.get_value("resilience.circuit_breaker.recovery_timeout", 30.0)
        self._success_threshold = success_threshold if success_threshold is not None else cfg.get_value("resilience.circuit_breaker.success_threshold", 3)

        self._state = State.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: float = 0.0
        self._lock = threading.RLock()

    @property
    def state(self) -> State:
        """Текущее состояние."""
        with self._lock:
            if self._state == State.OPEN:
                if time.monotonic() - self._last_failure_time >= self._recovery_timeout:
                    self._transition(State.HALF_OPEN)
            return self._state

    def _transition(self, new_state: State) -> None:
        """Переход в новое состояние с логированием."""
        old = self._state
        self._state = new_state
        log.info(
            "Circuit breaker state changed",
            extra={"from": old.value, "to": new_state.value},
        )

    def call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Выполнить функцию через circuit breaker.

        Args:
            fn: Целевая функция.
            *args: Позиционные аргументы.
            **kwargs: Именованные аргументы.

        Returns:
            Результат функции.

        Raises:
            CircuitOpenError: Если circuit breaker в состоянии OPEN.
            Exception: Ошибка целевой функции.
        """
        current_state = self.state
        if current_state == State.OPEN:
            from core.errors import CircuitOpenError

            raise CircuitOpenError("Circuit breaker is OPEN")

        try:
            result = fn(*args, **kwargs)
        except Exception:
            self._record_failure()
            raise
        else:
            self._record_success()
            return result

    def _record_failure(self) -> None:
        """Зафиксировать ошибку."""
        with self._lock:
            self._failure_count += 1
            self._success_count = 0
            self._last_failure_time = time.monotonic()

            log.warning(
                "Circuit breaker failure recorded",
                extra={"failures": self._failure_count, "threshold": self._failure_threshold},
            )

            if self._failure_count >= self._failure_threshold:
                self._transition(State.OPEN)

    def _record_success(self) -> None:
        """Зафиксировать успех."""
        with self._lock:
            if self._state == State.HALF_OPEN:
                self._success_count += 1
                log.info(
                    "Circuit breaker success in HALF_OPEN",
                    extra={"successes": self._success_count, "threshold": self._success_threshold},
                )
                if self._success_count >= self._success_threshold:
                    self._failure_count = 0
                    self._success_count = 0
                    self._transition(State.CLOSED)
            else:
                self._failure_count = 0
