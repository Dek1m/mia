"""Утиный Future над Celery AsyncResult: result / done / exception."""
from __future__ import annotations

import json
import time
from typing import Any, Callable

from argenta_logging import get_logger

from core.dispatch.envelope import TaskResult
from core.dispatch.errors import REDIS_PROTOCOL, TASK_FAILED, DispatchError
from core.dispatch.secret_box import SecretBox

log = get_logger(__name__)

_REDIS_PROTOCOL_TYPES = frozenset({"ProtocolError", "InvalidResponse"})
_POLL_INTERVAL = 0.05


class TaskResultHandle:
    """Обёртка Celery AsyncResult. Не логирует plaintext/ciphertext."""

    def __init__(
        self,
        async_result: Any,
        box: SecretBox,
        timeout: float | None = None,
        lock: Any = None,
    ) -> None:
        self._async_result = async_result
        self._box = box
        self._timeout = timeout
        self._lock = lock
        self._value: Any = None
        self._error: BaseException | None = None
        self._consumed = False

    def result(self, timeout: float | None = None) -> Any:
        self._consume(timeout)
        if self._error is not None:
            raise self._error
        return self._value

    def done(self) -> bool:
        if self._consumed:
            return True
        ready = getattr(self._async_result, "ready", None)
        return bool(ready()) if callable(ready) else False

    def exception(self, timeout: float | None = None) -> BaseException | None:
        try:
            self.result(timeout=timeout)
        except Exception as exc:
            return exc
        return None

    def cancelled(self) -> bool:
        cancelled = getattr(self._async_result, "cancelled", None)
        return bool(cancelled()) if callable(cancelled) else False

    def _consume(self, timeout: float | None) -> None:
        if self._consumed:
            return
        wait = self._timeout if timeout is None else timeout
        try:
            raw = self._get_raw(wait)
        except Exception as exc:
            self._error = _dispatch_from_transport(exc)
            self._consumed = True
            return
        self._unpack(raw)
        self._consumed = True

    def _get_raw(self, wait: float | None) -> Any:
        if self._lock is None:
            return self._async_result.get(timeout=wait)
        deadline = None if wait is None else time.monotonic() + wait
        while True:
            if self._redis(self._async_result.ready):
                return self._redis(self._async_result.get, timeout=0)
            if deadline is None:
                time.sleep(_POLL_INTERVAL)
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._redis(self._async_result.get, timeout=0)
            time.sleep(min(_POLL_INTERVAL, remaining))

    def _redis(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        # redis-py Connection не thread-safe: lock только на round-trip, wait — снаружи
        with self._lock:
            return fn(*args, **kwargs)

    def _unpack(self, raw: Any) -> None:
        if not isinstance(raw, dict):
            self._error = DispatchError(TASK_FAILED, "worker returned non-dict result")
            return
        parsed = TaskResult.from_dict(raw)
        if not parsed.ok:
            err = parsed.error
            code = err.code if err else TASK_FAILED
            message = err.message if err else "task failed"
            self._error = DispatchError(code, message)
            return
        if not parsed.result_enc:
            self._error = DispatchError(TASK_FAILED, "missing result_enc")
            return
        try:
            self._value = json.loads(self._box.decrypt(parsed.result_enc))
        except DispatchError as exc:
            self._error = exc
        except Exception as exc:
            self._error = DispatchError(TASK_FAILED, f"{type(exc).__name__}: {exc}")


def _is_redis_protocol(exc: BaseException) -> bool:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if type(current).__name__ in _REDIS_PROTOCOL_TYPES:
            return True
        if "Protocol Error" in str(current):
            return True
        current = current.__cause__ or current.__context__
    return False


def _dispatch_from_transport(exc: BaseException) -> DispatchError:
    error_type = type(exc).__name__
    error_message = str(exc)
    code = REDIS_PROTOCOL if _is_redis_protocol(exc) else TASK_FAILED
    message = error_message if code == REDIS_PROTOCOL else f"{error_type}: {error_message}"
    log.error(
        "task_result_failed",
        extra={
            "error_type": error_type,
            "error_message": error_message,
            "code": code,
        },
    )
    return DispatchError(code, message)
