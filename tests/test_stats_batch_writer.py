"""Unit-тесты для StatsBatchWriter — батчевая запись статистики задач."""
import asyncio
import threading
import time
from unittest.mock import MagicMock

import pytest

from core.task import Task, TaskStatus, TaskType
from core.stats_batch_writer import StatsBatchWriter, _percentile


class TestStatsBatchWriterAdd:
    """Добавление задач в буфер."""

    def test_add_task(self):
        """add() добавляет задачу в буфер."""
        db = MagicMock()
        writer = StatsBatchWriter(db, batch_size=10)
        task = Task.create(module_id="db", fn_name="get_user")
        writer.add(task)
        assert writer.buffer_size() == 1

    def test_add_multiple(self):
        """Несколько задач в буфере."""
        db = MagicMock()
        writer = StatsBatchWriter(db, batch_size=10)
        for i in range(5):
            task = Task.create(module_id="db", fn_name=f"f{i}")
            writer.add(task)
        assert writer.buffer_size() == 5

    def test_add_sets_event_on_batch_size(self):
        """add() устанавливает event при достижении batch_size."""
        db = MagicMock()
        writer = StatsBatchWriter(db, batch_size=3)
        for i in range(2):
            task = Task.create(module_id="db", fn_name=f"f{i}")
            writer.add(task)
        assert not writer._flush_event.is_set()
        task = Task.create(module_id="db", fn_name="f2")
        writer.add(task)
        assert writer._flush_event.is_set()

    def test_add_preserves_task_fields(self):
        """add() корректно сериализует поля задачи."""
        db = MagicMock()
        writer = StatsBatchWriter(db, batch_size=10)
        task = Task.create(module_id="db", fn_name="get_user", task_type=TaskType.IO)
        task.start()
        task.complete(result="ok")
        writer.add(task)

        row = writer._buffer[0]
        assert row["module_id"] == "db"
        assert row["fn_name"] == "get_user"
        assert row["task_type"] == "io"
        assert row["status"] == "completed"
        assert row["duration_ms"] is not None
        assert row["duration_ms"] > 0


class TestStatsBatchWriterFlush:
    """Flush буфера."""

    def test_flush_empty(self):
        """flush() на пустом буфере — ничего не делает."""
        db = MagicMock()
        writer = StatsBatchWriter(db)
        asyncio.get_event_loop().run_until_complete(writer.flush())
        db.execute.assert_not_called()

    def test_flush_inserts_history(self):
        """flush() вставляет записи в task_history."""
        db = MagicMock()
        writer = StatsBatchWriter(db)
        for i in range(3):
            task = Task.create(module_id="db", fn_name=f"f{i}")
            task.start()
            task.complete(result=f"r{i}")
            writer.add(task)
        asyncio.get_event_loop().run_until_complete(writer.flush())
        assert db.execute.call_count >= 1
        first_call = db.execute.call_args_list[0]
        assert "INSERT INTO task_history" in first_call[0][0]

    def test_flush_updates_stats(self):
        """flush() обновляет task_stats."""
        db = MagicMock()
        writer = StatsBatchWriter(db)
        task = Task.create(module_id="db", fn_name="f1")
        task.start()
        task.complete()
        writer.add(task)
        asyncio.get_event_loop().run_until_complete(writer.flush())
        # INSERT history + UPSERT stats
        assert db.execute.call_count == 2
        stats_call = db.execute.call_args_list[1]
        assert "task_stats" in stats_call[0][0]

    def test_flush_clears_buffer(self):
        """flush() очищает буфер после записи."""
        db = MagicMock()
        writer = StatsBatchWriter(db)
        task = Task.create(module_id="db", fn_name="f1")
        writer.add(task)
        assert writer.buffer_size() == 1
        asyncio.get_event_loop().run_until_complete(writer.flush())
        assert writer.buffer_size() == 0

    def test_flush_groups_by_module_and_type(self):
        """flush() группирует задачи по (module_id, task_type) для task_stats."""
        db = MagicMock()
        writer = StatsBatchWriter(db)

        t1 = Task.create(module_id="db", fn_name="f1")
        t1.task_type = TaskType.IO
        t1.start()
        t1.complete()
        writer.add(t1)

        t2 = Task.create(module_id="cache", fn_name="f2")
        t2.task_type = TaskType.IO
        t2.start()
        t2.complete()
        writer.add(t2)

        asyncio.get_event_loop().run_until_complete(writer.flush())

        # 1 INSERT history + 2 UPSERT stats (разные module_id)
        stats_calls = [c for c in db.execute.call_args_list if "task_stats" in c[0][0]]
        assert len(stats_calls) == 2

    def test_flush_batch_insert_params(self):
        """flush() формирует корректный batch INSERT с параметрами."""
        db = MagicMock()
        writer = StatsBatchWriter(db)

        t1 = Task.create(module_id="db", fn_name="f1")
        t1.start()
        t1.complete()
        writer.add(t1)

        t2 = Task.create(module_id="db", fn_name="f2")
        t2.start()
        t2.complete()
        writer.add(t2)

        asyncio.get_event_loop().run_until_complete(writer.flush())

        # Проверяем что INSERT содержит 2 строки значений
        history_call = db.execute.call_args_list[0]
        query = history_call[0][0]
        assert "VALUES" in query
        assert query.count("($") == 2  # 2 строки


class TestStatsBatchWriterAutoFlush:
    """Автоматический flush при достижении batch_size."""

    def test_auto_flush_on_batch_size(self):
        """flush() при достижении batch_size."""
        db = MagicMock()
        writer = StatsBatchWriter(db, batch_size=3)
        writer.start()
        try:
            time.sleep(0.05)  # Дать потоку запуститься
            for i in range(3):
                task = Task.create(module_id="db", fn_name=f"f{i}")
                task.start()
                task.complete()
                writer.add(task)
            time.sleep(0.5)
            assert writer.buffer_size() == 0
            assert db.execute.call_count >= 1
        finally:
            writer.stop()

    def test_no_flush_below_batch_size(self):
        """Нет flush пока buffer < batch_size (без таймера)."""
        db = MagicMock()
        writer = StatsBatchWriter(db, batch_size=10, flush_interval=10.0)
        writer.start()
        try:
            time.sleep(0.05)
            for i in range(3):
                task = Task.create(module_id="db", fn_name=f"f{i}")
                writer.add(task)
            time.sleep(0.2)
            # Буфер не должен быть очищен — flush_interval слишком длинный
            assert writer.buffer_size() == 3
        finally:
            writer.stop()


class TestStatsBatchWriterTimer:
    """Flush по таймеру."""

    def test_flush_by_timer(self):
        """flush() через flush_interval."""
        db = MagicMock()
        writer = StatsBatchWriter(db, flush_interval=0.1)
        writer.start()
        try:
            task = Task.create(module_id="db", fn_name="f1")
            task.start()
            task.complete()
            writer.add(task)
            time.sleep(0.5)
            assert writer.buffer_size() == 0
            assert db.execute.call_count >= 1
        finally:
            writer.stop()

    def test_timer_does_not_flush_empty_buffer(self):
        """Таймер не вызывает flush на пустом буфере."""
        db = MagicMock()
        writer = StatsBatchWriter(db, flush_interval=0.1)
        writer.start()
        try:
            time.sleep(0.35)
            db.execute.assert_not_called()
        finally:
            writer.stop()


class TestStatsBatchWriterStop:
    """Остановка."""

    def test_stop_flushes_remaining(self):
        """stop() записывает оставшиеся задачи."""
        db = MagicMock()
        writer = StatsBatchWriter(db, batch_size=100)
        task = Task.create(module_id="db", fn_name="f1")
        task.start()
        task.complete()
        writer.add(task)
        writer.start()
        writer.stop()
        assert writer.buffer_size() == 0
        assert db.execute.call_count >= 1

    def test_stop_sets_running_false(self):
        """stop() устанавливает _running = False."""
        db = MagicMock()
        writer = StatsBatchWriter(db)
        writer.start()
        assert writer._running is True
        writer.stop()
        assert writer._running is False

    def test_stop_without_start(self):
        """stop() без start() — ничего не делает."""
        db = MagicMock()
        writer = StatsBatchWriter(db)
        writer.stop()
        assert writer._running is False

    def test_stop_is_idempotent(self):
        """Двойной stop() не падает."""
        db = MagicMock()
        writer = StatsBatchWriter(db)
        writer.start()
        writer.stop()
        writer.stop()  # Should not raise

    def test_stop_joins_thread(self):
        """stop() дожидается завершения потока."""
        db = MagicMock()
        writer = StatsBatchWriter(db)
        writer.start()
        writer.stop()
        assert writer._thread is None or not writer._thread.is_alive()


class TestStatsBatchWriterConcurrency:
    """Конкурентный доступ."""

    def test_concurrent_add(self):
        """Конкурентное добавление задач."""
        db = MagicMock()
        writer = StatsBatchWriter(db, batch_size=100)
        errors = []

        def add_tasks(prefix: str, count: int):
            try:
                for i in range(count):
                    task = Task.create(module_id="db", fn_name=f"{prefix}_{i}")
                    writer.add(task)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=add_tasks, args=("t1", 50)),
            threading.Thread(target=add_tasks, args=("t2", 50)),
            threading.Thread(target=add_tasks, args=("t3", 50)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert writer.buffer_size() == 150

    def test_concurrent_add_and_flush(self):
        """Конкурентные add() и flush()."""
        db = MagicMock()
        writer = StatsBatchWriter(db, batch_size=100)
        errors = []

        def add_tasks():
            try:
                for i in range(50):
                    task = Task.create(module_id="db", fn_name=f"f{i}")
                    task.start()
                    task.complete()
                    writer.add(task)
                    time.sleep(0.001)
            except Exception as e:
                errors.append(e)

        def flush_tasks():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                for _ in range(10):
                    loop.run_until_complete(writer.flush())
                    time.sleep(0.01)
                loop.close()
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=add_tasks),
            threading.Thread(target=add_tasks),
            threading.Thread(target=flush_tasks),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors


class TestPercentile:
    """Вычисление перцентиля."""

    def test_percentile_empty(self):
        """Пустой список."""
        assert _percentile([], 0.95) == 0.0

    def test_percentile_single(self):
        """Один элемент."""
        assert _percentile([10.0], 0.95) == 10.0

    def test_percentile_95th(self):
        """95-й перцентиль для 1..100: idx=int(100*0.95)=95 → значение 96 (0-indexed)."""
        data = list(range(1, 101))
        assert _percentile(data, 0.95) == 96

    def test_percentile_50th(self):
        """50-й перцентиль для 1..100: idx=int(100*0.50)=50 → значение 51 (0-indexed)."""
        data = list(range(1, 101))
        assert _percentile(data, 0.50) == 51

    def test_percentile_unsorted(self):
        """Перцентиль сортирует данные."""
        data = [100, 1, 50, 25, 75]
        result = _percentile(data, 0.95)
        assert result == 100.0

    def test_percentile_small_batch(self):
        """Перцентиль для маленького батча."""
        data = [10.0, 20.0, 30.0]
        assert _percentile(data, 0.95) == 30.0
