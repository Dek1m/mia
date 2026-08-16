"""TaskStore — in-memory хранилище задач с ring buffer для истории."""
from __future__ import annotations

import threading
from collections import deque
from typing import Any
from uuid import UUID

from core.task import Task, TaskStatus

_SENTINEL = object()  # для различения None и "не передано"

# Прямой импорт метрик — zero-overhead при отсутствии prometheus_client
try:
    from monitoring.metrics import task_store_size as _task_store_size_gauge
except ImportError:
    _task_store_size_gauge = None


class TaskStore:
    """Потокобезопасное хранилище задач.

    Активные задачи (PENDING, RUNNING) хранятся в dict.
    Завершённые (COMPLETED, FAILED, TIMEOUT) — в ring buffer (deque, maxlen=25000).
    """

    def __init__(self, max_size: int | None = None) -> None:
        if max_size is None:
            from core.config import MiaConfig
            max_size = MiaConfig.get().get_value("core.task_store.max_size", 25000)
        self._active: dict[UUID, Task] = {}
        self._history: deque[Task] = deque(maxlen=max_size)
        self._lock = threading.RLock()

    def add(self, task: Task) -> None:
        """Добавить задачу в active."""
        with self._lock:
            self._active[task.id] = task
            self._update_gauge()

    def start(self, task: Task) -> None:
        """Пометить задачу как RUNNING."""
        with self._lock:
            if task.id in self._active:
                task.start()

    def complete(self, task: Task, result: Any = _SENTINEL) -> None:
        """Завершить задачу успешно и переместить в history.

        Если ``result`` передан — записывается в задачу через ``task.complete(result)``.
        """
        with self._lock:
            if result is not _SENTINEL:
                task.complete(result)
            else:
                task.complete()
            self._active.pop(task.id, None)
            self._history.append(task)
            self._update_gauge()

    def fail(self, task: Task, error: str) -> None:
        """Завершить задачу с ошибкой и переместить в history."""
        with self._lock:
            task.fail(error)
            self._active.pop(task.id, None)
            self._history.append(task)
            self._update_gauge()

    def get(self, task_id: UUID) -> Task | None:
        """Найти задачу по ID (сначала active, потом history)."""
        with self._lock:
            task = self._active.get(task_id)
            if task is not None:
                return task
            for t in reversed(self._history):
                if t.id == task_id:
                    return t
            return None

    def get_active(self) -> list[Task]:
        """Вернуть список всех активных задач."""
        with self._lock:
            return list(self._active.values())

    def get_history(self, limit: int | None = None) -> list[Task]:
        """Вернуть последние limit завершённых задач (новейшие первые)."""
        if limit is None:
            from core.config import MiaConfig
            limit = MiaConfig.get().get_value("core.task_store.history_limit", 100)
        with self._lock:
            return list(reversed(list(self._history)[-limit:]))

    def _update_gauge(self) -> None:
        """Обновить gauge метрику размера store."""
        if _task_store_size_gauge is not None:
            _task_store_size_gauge.set(len(self._active) + len(self._history))

    def stats(self) -> dict:
        """Статистика по задачам."""
        with self._lock:
            total = len(self._active) + len(self._history)
            completed = sum(
                1 for t in self._history if t.status == TaskStatus.COMPLETED
            )
            failed = sum(
                1 for t in self._history if t.status == TaskStatus.FAILED
            )
            timeout = sum(
                1 for t in self._history if t.status == TaskStatus.TIMEOUT
            )
            return {
                "total": total,
                "active": len(self._active),
                "completed": completed,
                "failed": failed,
                "timeout": timeout,
                "history_size": len(self._history),
            }
