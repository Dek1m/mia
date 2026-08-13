"""Unit-тесты для TaskStore — in-memory хранилище задач."""
import threading
import time
from uuid import uuid4

import pytest

from core.task import Task, TaskStatus, TaskType
from core.task_store import TaskStore


class TestTaskStoreAdd:
    """Добавление задач."""

    def test_add_task(self):
        """add() добавляет задачу в active."""
        store = TaskStore()
        task = Task.create(module_id="db", fn_name="get_user")
        store.add(task)
        assert task.id in store._active

    def test_add_multiple(self):
        """Несколько задач в active."""
        store = TaskStore()
        t1 = Task.create(module_id="db", fn_name="f1")
        t2 = Task.create(module_id="db", fn_name="f2")
        store.add(t1)
        store.add(t2)
        assert len(store.get_active()) == 2


class TestTaskStoreStatusSwitch:
    """Переключение статусов."""

    def test_start_task(self):
        """start() помечает задачу как RUNNING."""
        store = TaskStore()
        task = Task.create(module_id="db", fn_name="query")
        store.add(task)
        store.start(task)
        assert task.status == TaskStatus.RUNNING

    def test_complete_task(self):
        """complete() перемещает задачу в history."""
        store = TaskStore()
        task = Task.create(module_id="db", fn_name="get")
        store.add(task)
        store.start(task)
        store.complete(task)
        assert task.status == TaskStatus.COMPLETED
        assert task.id not in store._active
        assert task in store._history
        assert task.result is None

    def test_fail_task(self):
        """fail() перемещает задачу в history с ошибкой."""
        store = TaskStore()
        task = Task.create(module_id="db", fn_name="write")
        store.add(task)
        store.start(task)
        store.fail(task, "connection refused")
        assert task.status == TaskStatus.FAILED
        assert task.error == "connection refused"
        assert task.id not in store._active
        assert task in store._history

    def test_complete_without_start(self):
        """complete() без start() — задача завершается."""
        store = TaskStore()
        task = Task.create(module_id="x", fn_name="y")
        store.add(task)
        store.complete(task)
        assert task.status == TaskStatus.COMPLETED
        assert task.id not in store._active

    def test_fail_without_start(self):
        """fail() без start() — задача завершается с ошибкой."""
        store = TaskStore()
        task = Task.create(module_id="x", fn_name="y")
        store.add(task)
        store.fail(task, "err")
        assert task.status == TaskStatus.FAILED


class TestTaskStoreGet:
    """Поиск задач."""

    def test_get_active(self):
        """get() находит задачу в active."""
        store = TaskStore()
        task = Task.create(module_id="db", fn_name="q")
        store.add(task)
        found = store.get(task.id)
        assert found is task

    def test_get_history(self):
        """get() находит задачу в history."""
        store = TaskStore()
        task = Task.create(module_id="db", fn_name="q")
        store.add(task)
        store.complete(task)
        found = store.get(task.id)
        assert found is task

    def test_get_not_found(self):
        """get() возвращает None для несуществующего ID."""
        store = TaskStore()
        assert store.get(uuid4()) is None

    def test_get_active_list(self):
        """get_active() возвращает список активных."""
        store = TaskStore()
        t1 = Task.create(module_id="a", fn_name="f1")
        t2 = Task.create(module_id="a", fn_name="f2")
        store.add(t1)
        store.add(t2)
        active = store.get_active()
        assert len(active) == 2
        assert t1 in active
        assert t2 in active

    def test_get_history_limit(self):
        """get_history(limit) возвращает не более limit записей."""
        store = TaskStore()
        for i in range(10):
            t = Task.create(module_id="db", fn_name=f"f{i}")
            store.add(t)
            store.complete(t)
        history = store.get_history(limit=5)
        assert len(history) == 5

    def test_get_history_order(self):
        """get_history() возвращает новейшие первыми."""
        store = TaskStore()
        tasks = []
        for i in range(5):
            t = Task.create(module_id="db", fn_name=f"f{i}")
            store.add(t)
            store.complete(t)
            tasks.append(t)
        history = store.get_history()
        assert history == list(reversed(tasks))


class TestTaskStoreOverflow:
    """Overflow ring buffer."""

    def test_history_overflow(self):
        """При превышении max_size старые задачи удаляются."""
        store = TaskStore(max_size=5)
        tasks = []
        for i in range(10):
            t = Task.create(module_id="db", fn_name=f"f{i}")
            store.add(t)
            store.complete(t)
            tasks.append(t)
        history = store.get_history(limit=100)
        assert len(history) == 5
        # get_history() возвращает новейшие первыми → reverse ожидаемого порядка
        assert history == list(reversed(tasks[5:]))

    def test_overflow_preserves_active(self):
        """Overflow не затрагивает active задачи."""
        store = TaskStore(max_size=3)
        active_task = Task.create(module_id="db", fn_name="keep")
        store.add(active_task)
        for i in range(5):
            t = Task.create(module_id="db", fn_name=f"f{i}")
            store.add(t)
            store.complete(t)
        assert active_task.id in store._active
        assert store.get(active_task.id) is active_task


class TestTaskStoreThreadSafety:
    """Thread-safety."""

    def test_concurrent_add(self):
        """Конкурентное добавление задач."""
        store = TaskStore()
        errors = []

        def add_tasks(prefix: str, count: int):
            try:
                for i in range(count):
                    t = Task.create(module_id="db", fn_name=f"{prefix}_{i}")
                    store.add(t)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=add_tasks, args=("t1", 100)),
            threading.Thread(target=add_tasks, args=("t2", 100)),
            threading.Thread(target=add_tasks, args=("t3", 100)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(store.get_active()) == 300

    def test_concurrent_complete(self):
        """Конкурентное завершение задач."""
        store = TaskStore()
        tasks = []
        for i in range(100):
            t = Task.create(module_id="db", fn_name=f"f{i}")
            store.add(t)
            tasks.append(t)
        errors = []

        def complete_tasks(task_list: list[Task]):
            try:
                for t in task_list:
                    store.complete(t)
            except Exception as e:
                errors.append(e)

        half = len(tasks) // 2
        threads = [
            threading.Thread(target=complete_tasks, args=(tasks[:half],)),
            threading.Thread(target=complete_tasks, args=(tasks[half:],)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(store.get_active()) == 0
        assert len(store._history) == 100

    def test_concurrent_mixed(self):
        """Конкурентные add, complete, get."""
        store = TaskStore()
        errors = []

        def writer(start: int, count: int):
            try:
                for i in range(start, start + count):
                    t = Task.create(module_id="db", fn_name=f"f{i}")
                    store.add(t)
                    store.start(t)
                    store.complete(t)
            except Exception as e:
                errors.append(e)

        def reader():
            try:
                for _ in range(50):
                    store.get_active()
                    store.get_history(limit=10)
                    store.stats()
                    time.sleep(0.001)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=writer, args=(0, 50)),
            threading.Thread(target=writer, args=(50, 50)),
            threading.Thread(target=reader),
            threading.Thread(target=reader),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors


class TestTaskStoreStats:
    """Статистика."""

    def test_stats_empty(self):
        """stats() на пустом store."""
        store = TaskStore()
        s = store.stats()
        assert s["total"] == 0
        assert s["active"] == 0
        assert s["completed"] == 0
        assert s["failed"] == 0
        assert s["timeout"] == 0
        assert s["history_size"] == 0

    def test_stats_mixed(self):
        """stats() с разными статусами."""
        store = TaskStore()
        # 2 active
        t1 = Task.create(module_id="db", fn_name="f1")
        t2 = Task.create(module_id="db", fn_name="f2")
        store.add(t1)
        store.add(t2)
        # 3 completed
        for i in range(3):
            t = Task.create(module_id="db", fn_name=f"c{i}")
            store.add(t)
            store.complete(t)
        # 2 failed
        for i in range(2):
            t = Task.create(module_id="db", fn_name=f"e{i}")
            store.add(t)
            store.fail(t, f"err{i}")
        # 1 timeout
        t = Task.create(module_id="db", fn_name="to")
        store.add(t)
        t.start()
        t.timeout()
        store._active.pop(t.id, None)
        store._history.append(t)

        s = store.stats()
        assert s["total"] == 8
        assert s["active"] == 2
        assert s["completed"] == 3
        assert s["failed"] == 2
        assert s["timeout"] == 1
        assert s["history_size"] == 6

    def test_stats_after_overflow(self):
        """stats() корректен после overflow."""
        store = TaskStore(max_size=5)
        for i in range(10):
            t = Task.create(module_id="db", fn_name=f"f{i}")
            store.add(t)
            store.complete(t)
        s = store.stats()
        assert s["completed"] == 5
        assert s["history_size"] == 5
        assert s["total"] == 5
