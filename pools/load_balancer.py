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


class LoadBalancer:
    """Weighted scoring: score = 0.7 × cpu_load + 0.2 × active_tasks + 0.1 × stale_penalty.

    Чем меньше score — тем лучше воркер.
    """

    WEIGHT_CPU = 0.7
    WEIGHT_TASKS = 0.2
    WEIGHT_STALE = 0.1
    MAX_ACTIVE_TASKS = 10

    def __init__(self) -> None:
        self._workers: dict[int, WorkerState] = {}

    def select_worker(self, workers: dict[int, WorkerState]) -> int | None:
        """Выбрать воркер с наименьшим score.

        Args:
            workers: dict[worker_id, WorkerState].

        Returns:
            worker_id или None если нет доступных.
        """
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

    def _score(self, state: WorkerState) -> float:
        """Вычислить score воркера. Меньше — лучше."""
        normalized_tasks = min(state.active_tasks / self.MAX_ACTIVE_TASKS, 1.0)
        return (
            self.WEIGHT_CPU * state.cpu_load
            + self.WEIGHT_TASKS * normalized_tasks
            + self.WEIGHT_STALE * state.stale_penalty
        )
