"""ThreadPoolManager — управление пулом потоков."""
import threading
from concurrent.futures import ThreadPoolExecutor, Future
from typing import Any, Callable

from argenta_logging import get_logger

from monitoring.metrics import threadpool_active

log = get_logger(__name__)


class ThreadPoolManager:
    """Пул потоков для параллельного выполнения задач.

    Args:
        max_workers: Максимальное количество потоков.
    """

    def __init__(self, max_workers: int | None = None) -> None:
        self._max_workers = max_workers
        self._executor: ThreadPoolExecutor | None = None
        self._lock = threading.Lock()
        log.info("ThreadPoolManager created", extra={"max_workers": max_workers})

    def start(self) -> None:
        """Запустить пул потоков."""
        with self._lock:
            if self._executor is not None:
                log.warning("ThreadPool already started")
                return
            self._executor = ThreadPoolExecutor(max_workers=self._max_workers)
            threadpool_active.set(self._max_workers or 0)
            log.info("ThreadPool started", extra={"max_workers": self._max_workers})

    def submit(self, fn: Callable, *args: Any, **kwargs: Any) -> Future:
        """Отправить задачу на выполнение.

        Args:
            fn: Функция для выполнения.
            *args: Позиционные аргументы.
            **kwargs: Именованные аргументы.

        Returns:
            Future объект.

        Raises:
            RuntimeError: Если пул не запущен.
        """
        if self._executor is None:
            raise RuntimeError("ThreadPool not started. Call start() first.")
        log.debug("Task submitted", extra={"fn": fn.__name__})
        return self._executor.submit(fn, *args, **kwargs)

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
            threadpool_active.set(0)
            log.info("ThreadPool stopped")

    @property
    def active_workers(self) -> int:
        """Количество активных потоков."""
        return self._max_workers or 0
