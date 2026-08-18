"""LoadBalancer — балансировщик нагрузки на основе weighted scoring."""
from __future__ import annotations

from dataclasses import dataclass, field
from monitoring.metrics import (
    loadbalancer_score,
    loadbalancer_selections_total,
    loadbalancer_no_worker_total,
)
from argenta_logging import get_logger

log = get_logger(__name__)


@dataclass
class WorkerState:
    """Состояние воркера для балансировки."""

    worker_id: int
    pid: int
    cpu_load: float = 0.0
    active_tasks: int = 0
    stale_penalty: float = 0.0
    last_heartbeat: float = 0.0
    core_id: int = 0
    free_threads: int = 0
    max_threads: int = 1


class LoadBalancer:
    """Weighted scoring: score = 0.7 × cpu_load + 0.3 × (1 - free_ratio).

    Чем меньше score — тем лучше воркер.
    Учитывает свободные потоки: меньше свободных → хуже.
    """

    def __init__(self) -> None:
        from core.config import MiaConfig
        cfg = MiaConfig.get()
        self.WEIGHT_CPU = cfg.get_value("pools.load_balancer.weight_cpu", 0.7)
        self.WEIGHT_TASKS = cfg.get_value("pools.load_balancer.weight_tasks", 0.2)
        self.WEIGHT_STALE = cfg.get_value("pools.load_balancer.weight_stale", 0.1)
        self.MAX_ACTIVE_TASKS = cfg.get_value("pools.load_balancer.max_active_tasks", 10)
        self._workers: dict[int, WorkerState] = {}

    def select_worker(self, workers: dict[int, WorkerState] | None = None) -> int | None:
        """Выбрать воркер с наименьшим score.

        Args:
            workers: dict[worker_id, WorkerState]. Если None — использует внутренний кеш.

        Returns:
            worker_id или None если нет доступных.
        """
        if workers is None:
            workers = self._workers

        if not workers:
            loadbalancer_no_worker_total.inc()
            return None

        best_id: int | None = None
        best_score = float("inf")

        for wid, state in workers.items():
            score = self._score(state)
            loadbalancer_score.observe(score)

            if score < best_score:
                best_score = score
                best_id = wid

        if best_id is not None:
            loadbalancer_selections_total.labels(worker_id=str(best_id)).inc()
            log.debug("Worker selected", extra={"worker_id": best_id, "score": best_score})
        else:
            loadbalancer_no_worker_total.inc()

        return best_id

    def update_worker_state(self, worker_id: int, state: WorkerState) -> None:
        """Обновить состояние воркера."""
        self._workers[worker_id] = state

    def increment_active(self, worker_id: int) -> None:
        """Увеличить счётчик активных задач воркера."""
        state = self._workers.get(worker_id)
        if state is not None:
            state.active_tasks += 1

    def decrement_active(self, worker_id: int) -> None:
        """Уменьшить счётчик активных задач воркера."""
        state = self._workers.get(worker_id)
        if state is not None:
            state.active_tasks = max(0, state.active_tasks - 1)

    def _score(self, state: WorkerState) -> float:
        """Вычислить score воркера. Меньше — лучше.

        Формула: 0.7 × cpu_load + 0.3 × (1 - free_ratio).
        free_ratio = free_threads / max(max_threads, 1).
        Чем меньше свободных потоков — тем хуже (выше score).
        """
        free_ratio = state.free_threads / max(state.max_threads, 1)
        return (
            self.WEIGHT_CPU * state.cpu_load
            + 0.3 * (1 - free_ratio)
        )
