"""SharedMemoryManager — хранилище результатов задач по UUID."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from argenta_logging import get_logger
from core.interfaces import ISharedMemory

log = get_logger(__name__)


@dataclass(frozen=True)
class ResultEntry:
    """Запись результата в shared memory."""

    result: Any
    created_at: float = field(default_factory=time.monotonic)


class SharedMemoryManager(ISharedMemory):
    """Потокобезопасное хранилище результатов задач по UUID.

    Используется для передачи результатов от воркеров к вызывающему коду.
    Каждый результат хранится с TTL для автоматической очистки.

    Attributes:
        max_results: Максимальное количество хранимых результатов.
        ttl: Время жизни результата в секундах (0 = бессрочно).
    """

    def __init__(self, max_results: int = 25000, ttl: float = 300.0) -> None:
        from core.config import MiaConfig
        cfg = MiaConfig.get()
        self._max_results = cfg.get_value("core.shared_memory.max_results", max_results)
        self._ttl = cfg.get_value("core.shared_memory.ttl", ttl)
        self._results: dict[UUID, ResultEntry] = {}
        self._lock = threading.RLock()
        self._cleanup_thread: threading.Thread | None = None
        self._running = False

        if self._ttl > 0:
            self._start_cleanup_thread()

    def _start_cleanup_thread(self) -> None:
        """Запустить фоновый поток очистки просроченных результатов."""
        self._running = True
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop, daemon=True, name="shared-memory-cleanup",
        )
        self._cleanup_thread.start()

    def _cleanup_loop(self) -> None:
        """Фоновый цикл очистки просроченных результатов."""
        while self._running:
            time.sleep(min(self._ttl / 2, 30.0))
            self._cleanup_expired()

    def _cleanup_expired(self) -> None:
        """Удалить результаты, превысившие TTL."""
        if self._ttl <= 0:
            return
        now = time.monotonic()
        with self._lock:
            expired = [
                uid for uid, entry in self._results.items()
                if now - entry.created_at > self._ttl
            ]
            for uid in expired:
                del self._results[uid]

        if expired:
            log.debug("Cleaned up expired results", extra={"count": len(expired)})

    def set(self, task_id: UUID, result: Any) -> None:
        """Сохранить результат задачи.

        Args:
            task_id: UUID задачи.
            result: Результат выполнения.
        """
        with self._lock:
            if len(self._results) >= self._max_results:
                self._evict_oldest()
            self._results[task_id] = ResultEntry(result=result)

    def get(self, task_id: UUID) -> Any | None:
        """Получить результат задачи.

        Args:
            task_id: UUID задачи.

        Returns:
            Результат или None, если не найден.
        """
        with self._lock:
            entry = self._results.get(task_id)
            return entry.result if entry is not None else None

    def delete(self, task_id: UUID) -> bool:
        """Удалить результат задачи.

        Args:
            task_id: UUID задачи.

        Returns:
            True, если результат был удалён.
        """
        with self._lock:
            return self._results.pop(task_id, None) is not None

    def exists(self, task_id: UUID) -> bool:
        """Проверить наличие результата.

        Args:
            task_id: UUID задачи.

        Returns:
            True, если результат существует.
        """
        with self._lock:
            return task_id in self._results

    def clear(self) -> None:
        """Очистить все результаты."""
        with self._lock:
            self._results.clear()

    def _evict_oldest(self) -> None:
        """Удалить самый старый результат (FIFO)."""
        if not self._results:
            return
        oldest_key = min(self._results, key=lambda k: self._results[k].created_at)
        del self._results[oldest_key]

    @property
    def size(self) -> int:
        """Текущее количество хранимых результатов."""
        with self._lock:
            return len(self._results)

    def shutdown(self) -> None:
        """Остановить фоновый поток и очистить результаты."""
        self._running = False
        if self._cleanup_thread is not None:
            self._cleanup_thread.join(timeout=5.0)
            self._cleanup_thread = None
        self.clear()
        log.info("SharedMemoryManager stopped")
