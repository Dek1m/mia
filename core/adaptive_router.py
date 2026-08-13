"""AdaptiveRouter — динамическая маршрутизация на основе исторических данных."""
from __future__ import annotations

from collections import defaultdict
from statistics import quantiles

from core.task import Task, TaskType
from core.task_store import TaskStore

# Порог p95 duration (в секундах) для переключения типа
P95_THRESHOLD = 0.1  # 100ms

# Количество последних задач для анализа
HISTORY_WINDOW = 1000

# Карта переключений: текущий тип → рекомендуемый при превышении порога
_OVERLOAD_MAP: dict[TaskType, TaskType] = {
    TaskType.IO: TaskType.CPU,
    TaskType.CPU: TaskType.IO,
}


class AdaptiveRouter:
    """Маршрутизатор, корректирующий тип задачи по p95 duration.

    Анализирует последние HISTORY_WINDOW завершённых задач.
    Для каждой пары (module_id, task_type) считает p95 duration.
    Если p95 > P95_THRESHOLD — рекомендует альтернативный тип.
    """

    def __init__(self, task_store: TaskStore) -> None:
        self._store = task_store
        # (module_id, task_type) → p95 duration
        self._p95_stats: dict[tuple[str, TaskType], float] = {}
        self.update_stats()

    def update_stats(self) -> None:
        """Пересчитать p95 статистику из последних задач history."""
        history = self._store.get_history(limit=HISTORY_WINDOW)

        # Группируем duration по (module_id, task_type)
        buckets: dict[tuple[str, TaskType], list[float]] = defaultdict(list)
        for task in history:
            if task.duration is None:
                continue
            key = (task.module_id, task.task_type)
            buckets[key].append(task.duration)

        # Считаем p95 для каждого бакета
        self._p95_stats.clear()
        for key, durations in buckets.items():
            if len(durations) >= 2:
                p95 = quantiles(durations, n=100)[94]  # 95-й перцентиль
            else:
                p95 = durations[0]
            self._p95_stats[key] = p95

    def override(self, task: Task) -> TaskType | None:
        """Вернуть альтернативный тип, если p95 duration превышает порог.

        Returns:
            TaskType для маршрутизации в другой pool, или None если override не нужен.
        """
        key = (task.module_id, task.task_type)
        p95 = self._p95_stats.get(key)

        if p95 is None or p95 <= P95_THRESHOLD:
            return None

        return _OVERLOAD_MAP.get(task.task_type)

    def get_recommendations(self, module_id: str) -> dict[TaskType, TaskType | None]:
        """Рекомендации по типам задач для модуля.

        Returns:
            dict: {TaskType → recommended TaskType | None}
            None означает, что тип не требует переключения.
        """
        recommendations: dict[TaskType, TaskType | None] = {}
        for (mod, ttype), p95 in self._p95_stats.items():
            if mod != module_id:
                continue
            if p95 > P95_THRESHOLD and ttype in _OVERLOAD_MAP:
                recommendations[ttype] = _OVERLOAD_MAP[ttype]
            else:
                recommendations[ttype] = None
        return recommendations
