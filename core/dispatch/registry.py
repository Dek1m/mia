"""Реестр целей (module, method) → callable на воркере."""
from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from core.dispatch.async_bridge import run_async_sync
from core.dispatch.errors import METHOD_NOT_FOUND, DispatchError


class TaskTargetRegistry:
    """Карта (module, method) → callable. Bound @task получает self здесь."""

    def __init__(self) -> None:
        self._targets: dict[tuple[str, str], Callable[..., Any]] = {}

    def register(self, module: str, method: str, fn: Callable[..., Any]) -> None:
        self._targets[(module, method)] = fn

    def get(self, module: str, method: str) -> Callable[..., Any]:
        fn = self._targets.get((module, method))
        if fn is None:
            raise DispatchError(METHOD_NOT_FOUND, f"{module}.{method}")
        return fn

    def has(self, module: str, method: str) -> bool:
        return (module, method) in self._targets

    def register_object(self, module: str, obj: object) -> None:
        """Зарегистрировать @task-методы и _provider_* стабы объекта."""
        for name in dir(obj):
            if not _is_registerable_name(name):
                continue
            try:
                attr = getattr(obj, name)
            except Exception:
                continue
            if callable(attr) and _is_task_target(attr, name):
                self.register(module, name, adapt_target(attr, name))

    def register_database(self, database: Any) -> None:
        self.register_object("db", database)


def adapt_target(attr: Any, name: str) -> Callable[..., Any]:
    """Bound-метод: payload без self. Plain function — как есть."""
    instance = getattr(attr, "__self__", None)
    if instance is None:
        return attr
    if name.startswith("_provider_"):
        return attr
    raw = inspect.unwrap(attr)
    if inspect.iscoroutinefunction(raw):
        def async_call(*args: Any, **kwargs: Any) -> Any:
            return run_async_sync(raw, (instance, *args), kwargs)

        return async_call

    def sync_call(*args: Any, **kwargs: Any) -> Any:
        return raw(instance, *args, **kwargs)

    return sync_call


def _is_registerable_name(name: str) -> bool:
    if name.startswith("_provider_"):
        return True
    return not name.startswith("_")


def _is_task_target(attr: Any, name: str) -> bool:
    if name.startswith("_provider_"):
        return True
    if hasattr(attr, "_task_type"):
        return True
    func = getattr(attr, "__func__", None)
    return func is not None and hasattr(func, "_task_type")
