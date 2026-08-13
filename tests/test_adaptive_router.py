"""Unit-тесты для AdaptiveRouter — динамическая маршрутизация."""
from core.adaptive_router import HISTORY_WINDOW, P95_THRESHOLD, AdaptiveRouter
from core.task import Task, TaskType
from core.task_store import TaskStore


def _fill_history(store: TaskStore, tasks_with_duration: list[tuple[Task, float]]) -> None:
    """Добавить задачи в history с заданными duration.

    Args:
        tasks_with_duration: список кортежей (task, duration_seconds).
    """
    for task, duration in tasks_with_duration:
        store.add(task)
        store.start(task)
        store.complete(task)
        task.duration = duration


def _task(module_id: str, fn_name: str, task_type: TaskType) -> Task:
    return Task.create(module_id=module_id, fn_name=fn_name, task_type=task_type)


class TestAdaptiveRouterOverride:
    """Override на основе p95 статистики."""

    def test_no_data_returns_none(self):
        """Пустая history → override None."""
        store = TaskStore()
        router = AdaptiveRouter(store)
        task = _task("db", "get_user", TaskType.IO)
        assert router.override(task) is None

    def test_fast_tasks_no_override(self):
        """p95 < порога → override None."""
        store = TaskStore()
        _fill_history(store, [
            (_task("db", f"get_{i}", TaskType.IO), 0.05)
            for i in range(10)
        ])
        router = AdaptiveRouter(store)
        assert router.override(_task("db", "get_x", TaskType.IO)) is None

    def test_slow_io_overrides_to_cpu(self):
        """IO-задачи с p95 > порога → CPU."""
        store = TaskStore()
        _fill_history(store, [
            (_task("db", f"get_{i}", TaskType.IO), 0.2)
            for i in range(20)
        ])
        router = AdaptiveRouter(store)
        assert router.override(_task("db", "get_slow", TaskType.IO)) == TaskType.CPU

    def test_slow_cpu_overrides_to_io(self):
        """CPU-задачи с p95 > порога → IO."""
        store = TaskStore()
        _fill_history(store, [
            (_task("math", f"compute_{i}", TaskType.CPU), 0.5)
            for i in range(20)
        ])
        router = AdaptiveRouter(store)
        assert router.override(_task("math", "compute_heavy", TaskType.CPU)) == TaskType.IO

    def test_no_override_for_unmapped_types(self):
        """GPU/NETWORK/DATABASE не переключаются (нет в _OVERLOAD_MAP)."""
        store = TaskStore()
        _fill_history(store, [
            (_task("gpu", f"render_{i}", TaskType.GPU), 1.0)
            for i in range(20)
        ])
        router = AdaptiveRouter(store)
        assert router.override(_task("gpu", "render_slow", TaskType.GPU)) is None

    def test_module_isolation(self):
        """Статистика считается per module_id."""
        store = TaskStore()
        _fill_history(store, [
            *[
                (_task("db", f"get_{i}", TaskType.IO), 0.01)
                for i in range(20)
            ],
            *[
                (_task("math", f"compute_{i}", TaskType.CPU), 0.5)
                for i in range(20)
            ],
        ])
        router = AdaptiveRouter(store)

        # db: p95 < порога → None
        assert router.override(_task("db", "get_x", TaskType.IO)) is None
        # math: p95 > порога → IO
        assert router.override(_task("math", "compute_x", TaskType.CPU)) == TaskType.IO


class TestAdaptiveRouterUpdateStats:
    """Обновление статистики."""

    def test_update_recomputes_p95(self):
        """update_stats() пересчитывает p95."""
        store = TaskStore()
        _fill_history(store, [
            (_task("db", f"get_{i}", TaskType.IO), 0.01)
            for i in range(10)
        ])
        router = AdaptiveRouter(store)

        # Изначально p95 маленький → None
        assert router.override(_task("db", "get_x", TaskType.IO)) is None

        # Добавляем медленные задачи
        _fill_history(store, [
            (_task("db", f"get_slow_{i}", TaskType.IO), 0.3)
            for i in range(20)
        ])
        router.update_stats()

        # Теперь p95 > порога → CPU
        assert router.override(_task("db", "get_x", TaskType.IO)) == TaskType.CPU

    def test_stats_exclude_none_duration(self):
        """Задачи без duration не влияют на статистику."""
        store = TaskStore()
        task = _task("db", "get_x", TaskType.IO)
        store.add(task)
        store.complete(task)
        # duration = None (не вызывали start())

        router = AdaptiveRouter(store)
        assert router.override(task) is None

    def test_single_task_uses_its_duration(self):
        """При одной задаче p95 = её duration."""
        store = TaskStore()
        _fill_history(store, [(_task("db", "get_0", TaskType.IO), 0.5)])
        router = AdaptiveRouter(store)

        assert router.override(_task("db", "get_x", TaskType.IO)) == TaskType.CPU


class TestAdaptiveRouterRecommendations:
    """Рекомендации по модулям."""

    def test_recommendations_empty(self):
        """Пустая history → пустой dict."""
        store = TaskStore()
        router = AdaptiveRouter(store)
        assert router.get_recommendations("db") == {}

    def test_recommendations_fast_module(self):
        """Быстрый модуль → все типы без рекомендаций."""
        store = TaskStore()
        _fill_history(store, [
            (_task("db", f"get_{i}", TaskType.IO), 0.01)
            for i in range(10)
        ])
        router = AdaptiveRouter(store)

        recs = router.get_recommendations("db")
        assert recs == {TaskType.IO: None}

    def test_recommendations_slow_module(self):
        """Медленный модуль → рекомендация к переключению."""
        store = TaskStore()
        _fill_history(store, [
            (_task("db", f"get_{i}", TaskType.IO), 0.3)
            for i in range(20)
        ])
        router = AdaptiveRouter(store)

        recs = router.get_recommendations("db")
        assert recs == {TaskType.IO: TaskType.CPU}

    def test_recommendations_mixed_types(self):
        """Смешанные типы в одном модуле — каждому своя рекомендация."""
        store = TaskStore()
        _fill_history(store, [
            *[
                (_task("db", f"get_{i}", TaskType.IO), 0.3)
                for i in range(20)
            ],
            *[
                (_task("db", f"compute_{i}", TaskType.CPU), 0.01)
                for i in range(20)
            ],
        ])
        router = AdaptiveRouter(store)

        recs = router.get_recommendations("db")
        assert recs[TaskType.IO] == TaskType.CPU
        assert recs[TaskType.CPU] is None

    def test_recommendations_other_module_filtered(self):
        """Рекомендации возвращаются только для запрошенного модуля."""
        store = TaskStore()
        _fill_history(store, [
            *[
                (_task("db", f"get_{i}", TaskType.IO), 0.3)
                for i in range(20)
            ],
            *[
                (_task("math", f"compute_{i}", TaskType.CPU), 0.01)
                for i in range(20)
            ],
        ])
        router = AdaptiveRouter(store)

        recs_db = router.get_recommendations("db")
        recs_math = router.get_recommendations("math")

        assert recs_db == {TaskType.IO: TaskType.CPU}
        assert recs_math == {TaskType.CPU: None}
