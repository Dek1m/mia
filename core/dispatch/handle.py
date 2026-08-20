"""Утиный Future над Celery AsyncResult: result / done / exception."""
from __future__ import annotations

import json
from typing import Any

from core.dispatch.envelope import TaskResult
from core.dispatch.errors import TASK_FAILED, DispatchError
from core.dispatch.secret_box import SecretBox


class TaskResultHandle:
    """Обёртка Celery AsyncResult. Не логирует plaintext/ciphertext."""

    def __init__(
        self,
        async_result: Any,
        box: SecretBox,
        timeout: float | None = None,
    ) -> None:
        self._async_result = async_result
        self._box = box
        self._timeout = timeout
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
            raw = self._async_result.get(timeout=wait)
        except Exception as exc:
            self._error = DispatchError(TASK_FAILED, str(exc))
            self._consumed = True
            return
        self._unpack(raw)
        self._consumed = True

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
            self._error = DispatchError(TASK_FAILED, str(exc))
