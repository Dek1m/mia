"""Разбор аргументов dispatch(fn) / dispatch(task, fn)."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.task import Task


def parse_dispatch_args(first: Any, args: tuple[Any, ...]) -> tuple[Task, Callable[..., Any], tuple[Any, ...]]:
    """Извлечь Task, fn и call_args.

    dispatch(fn, *args)        → (auto_task, fn, args)
    dispatch(task, fn, *args)  → (task, fn, args)
    """
    if isinstance(first, Task):
        return first, args[0], args[1:]
    return (
        Task.create(
            module_id=getattr(first, "__module__", "unknown"),
            fn_name=getattr(first, "__name__", "unknown"),
        ),
        first,
        args,
    )
