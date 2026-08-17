"""WorkerThreadPool — ThreadPool внутри каждого воркера-процесса."""
from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable

from argenta_logging import get_logger
from core.interfaces import IWorkerThreadPool

log = get_logger(__name__)


class WorkerThreadPool(IWorkerThreadPool):
    """Пул потоков внутри worker-процесса.

    Каждый воркер создаёт свой ThreadPool для параллельного
    выполнения задач внутри процесса.

    Attributes:
        max_threads: Максимальное количество потоков в пуле.
    """

    def __init__(self, max_threads: int = 4) -> None:
        from core.config import MiaConfig
        cfg = MiaConfig.get()
        self._max_threads = cfg.get_value("pools.worker.thread_pool.max_threads", max_threads)
        self._executor: ThreadPoolExecutor | None = None
        self._lock = threading.Lock()
        self._active_count = 0
        self._active_lock = threading.Lock()

    def start(self) -> None:
        """Запустить пул потоков."""
        with self._lock:
            if self._executor is not None:
                log.warning("WorkerThreadPool already started")
                return
            self._executor = ThreadPoolExecutor(max_workers=self._max_threads)
            log.info("WorkerThreadPool started", extra={"max_threads": self._max_threads})

    def submit(self, fn: Callable, *args: Any, **kwargs: Any) -> Future:
        """Отправить задачу на выполнение.

        Args:
            fn: Функция для выполнения.
            *args: Позиционные аргументы.
            **kwargs: Именованные аргументы.

        Returns:
            Future с результатом выполнения.

        Raises:
            RuntimeError: Если пул не запущен.
        """
        if self._executor is None:
            raise RuntimeError("WorkerThreadPool not started. Call start() first.")

        with self._active_lock:
            self._active_count += 1

        def _wrapper() -> Any:
            try:
                return fn(*args, **kwargs)
            finally:
                with self._active_lock:
                    self._active_count -= 1

        return self._executor.submit(_wrapper)

    def shutdown(self, wait: bool = True) -> None:
        """Остановить пул потоков.

        Args:
            wait: Ждать завершения всех задач.
        """
        with self._lock:
            if self._executor is None:
                return
            self._executor.shutdown(wait=wait)
            self._executor = None
            log.info("WorkerThreadPool stopped")

    @property
    def active_count(self) -> int:
        """Количество активных задач."""
        with self._active_lock:
            return self._active_count

    @property
    def max_threads(self) -> int:
        """Максимальное количество потоков."""
        return self._max_threads
