"""Локальный диспетчер: вызывает fn в текущем процессе."""
from __future__ import annotations

import inspect
import threading
from concurrent.futures import Future
from typing import Any

from argenta_logging import get_logger

from core.dispatch.async_bridge import run_async_sync
from core.dispatch.parse import parse_dispatch_args
from core.interfaces import ISmartDispatcher

log = get_logger(__name__)


class LocalInvokeDispatcher(ISmartDispatcher):
    """Исполняет задачу в текущем процессе."""

    def __init__(self) -> None:
        self._write_lock = threading.Lock()

    def dispatch(self, first: Any, *args: Any, **kwargs: Any) -> Any:
        return self.dispatch_async(first, *args, **kwargs).result()

    def dispatch_async(self, first: Any, *args: Any, **kwargs: Any) -> Future:
        _task, fn, call_args = parse_dispatch_args(first, args)
        future: Future = Future()
        try:
            result = self._invoke(fn, call_args, kwargs)
        except Exception as exc:
            future.set_exception(exc)
        else:
            future.set_result(result)
        return future

    def acquire_lock(self) -> None:
        self._write_lock.acquire()

    def release_lock(self) -> None:
        self._write_lock.release()

    def _invoke(self, fn: Any, call_args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        target = inspect.unwrap(fn)
        if inspect.iscoroutinefunction(target):
            return run_async_sync(fn, call_args, kwargs)
        return fn(*call_args, **kwargs)
