"""Выполнить async-функцию синхронно (копия из старого smart_dispatcher)."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any


def run_async_sync(fn: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    """Если loop уже крутится — отдельный поток с новым loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(fn(*args, **kwargs))

    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, fn(*args, **kwargs)).result()
