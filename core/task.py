"""Task — универсальная задача для Universal Task System."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from uuid import UUID, uuid4


class TaskStatus(Enum):
    """Статус задачи в жизненном цикле."""

    PENDING = "pending"
    CLASSIFIED = "classified"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"


class TaskType(Enum):
    """Тип задачи для маршрутизации."""

    IO = "io"
    CPU = "cpu"
    GPU = "gpu"
    NETWORK = "network"
    DATABASE = "database"
    AGGREGATE = "aggregate"
    UNKNOWN = "unknown"


@dataclass
class Task:
    """Универсальная задача.

    Центральная сущность Universal Task System.
    Содержит всё необходимое для маршрутизации, выполнения и сбора метрик.
    """

    module_id: str
    task_type: TaskType
    fn_name: str
    status: TaskStatus = TaskStatus.PENDING
    id: UUID = field(default_factory=uuid4)
    created_at: float = field(default_factory=time.monotonic)
    started_at: float | None = None
    completed_at: float | None = None
    duration: float | None = None
    payload: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)
    result: Any = None
    error: str | None = None
    priority: int = 0

    @classmethod
    def create(
        cls,
        module_id: str,
        fn_name: str,
        task_type: TaskType = TaskType.UNKNOWN,
        payload: dict | None = None,
        metadata: dict | None = None,
        priority: int = 0,
    ) -> Task:
        """Фабричный метод — создаёт задачу из аргументов вызова."""
        return cls(
            module_id=module_id,
            fn_name=fn_name,
            task_type=task_type,
            payload=payload or {},
            metadata=metadata or {},
            priority=priority,
        )

    def start(self) -> None:
        """Пометить задачу как начатую."""
        self.status = TaskStatus.RUNNING
        self.started_at = time.monotonic()

    def complete(self, result: Any = None) -> None:
        """Пометить задачу как завершённую успешно."""
        self.status = TaskStatus.COMPLETED
        self.result = result
        self.completed_at = time.monotonic()
        if self.started_at is not None:
            self.duration = self.completed_at - self.started_at

    def fail(self, error: str) -> None:
        """Пометить задачу как завершённую с ошибкой."""
        self.status = TaskStatus.FAILED
        self.error = error
        self.completed_at = time.monotonic()
        if self.started_at is not None:
            self.duration = self.completed_at - self.started_at

    def timeout(self) -> None:
        """Пометить задачу как превысившую лимит времени."""
        self.status = TaskStatus.TIMEOUT
        self.completed_at = time.monotonic()
        if self.started_at is not None:
            self.duration = self.completed_at - self.started_at

    def to_dict(self) -> dict:
        """Сериализация в dict для JSON."""
        return {
            "id": str(self.id),
            "module_id": self.module_id,
            "task_type": self.task_type.value,
            "status": self.status.value,
            "fn_name": self.fn_name,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration": self.duration,
            "payload": self.payload,
            "metadata": self.metadata,
            "result": self.result,
            "error": self.error,
            "priority": self.priority,
        }
