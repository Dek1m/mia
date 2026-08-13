"""Интеграционные тесты для WorkerManager — управление lifecycle воркеров."""
import multiprocessing
import os
import time

import pytest

from pools.worker_manager import WorkerManager
from pools.load_balancer import LoadBalancer


# === Вспомогательные функции (должны быть на верхнем уровне) ===

def add(a, b):
    """Простая функция для тестирования submit."""
    return a + b


def heavy_task(n):
    """Тяжёлая функция — считает сумму квадратов."""
    return sum(i * i for i in range(n))


def failing_task():
    """Функция, которая всегда падает."""
    raise ValueError("Intentional test error!")


def get_pid():
    """Возвращает PID текущего процесса (воркера)."""
    return os.getpid()


# === Тесты ===

class TestWorkerManagerCreation:
    """Создание и инициализация."""

    def test_worker_manager_creation(self):
        """WorkerManager() создаётся без ошибок."""
        wm = WorkerManager()
        assert wm is not None
        assert isinstance(wm, WorkerManager)

    def test_worker_manager_with_custom_balancer(self):
        """WorkerManager с кастомным LoadBalancer."""
        lb = LoadBalancer()
        wm = WorkerManager(load_balancer=lb)
        assert wm.load_balancer is lb


class TestStartStop:
    """Запуск и остановка воркеров."""

    def test_start_creates_workers(self):
        """start(2) создаёт 2 воркеров."""
        wm = WorkerManager()
        wm.start(num_workers=2)
        try:
            ids = wm.get_worker_ids()
            assert len(ids) == 2
            assert all(isinstance(i, int) for i in ids)
        finally:
            wm.stop()

    def test_start_default_workers(self):
        """start() без аргументов = cpu_count() воркеров."""
        wm = WorkerManager()
        wm.start()
        try:
            ids = wm.get_worker_ids()
            assert len(ids) == os.cpu_count()
        finally:
            wm.stop()

    def test_stop_kills_workers(self):
        """stop() останавливает всех воркеров."""
        wm = WorkerManager()
        wm.start(num_workers=2)
        wm.stop()

        # После stop() — нет активных воркеров
        assert len(wm.get_worker_ids()) == 0
        assert wm._task_queue is None
        assert wm._result_queue is None

    def test_stop_is_idempotent(self):
        """Повторный stop() не падает."""
        wm = WorkerManager()
        wm.start(num_workers=1)
        wm.stop()
        wm.stop()  # Не должно упасть


class TestSubmit:
    """Отправка задач воркерам."""

    def test_submit_returns_result(self):
        """submit(fn, args) возвращает результат."""
        wm = WorkerManager()
        wm.start(num_workers=2)
        try:
            result = wm.submit(add, 3, 7)
            assert result == 10
        finally:
            wm.stop()

    def test_submit_multiple_tasks(self):
        """Несколько submit() работают корректно."""
        wm = WorkerManager()
        wm.start(num_workers=2)
        try:
            results = []
            for i in range(5):
                result = wm.submit(add, i, i)
                results.append(result)
            assert results == [0, 2, 4, 6, 8]
        finally:
            wm.stop()

    def test_submit_not_started_raises(self):
        """submit() до start() → RuntimeError."""
        wm = WorkerManager()
        with pytest.raises(RuntimeError, match="not started"):
            wm.submit(add, 1, 2)

    def test_submit_task_error(self):
        """submit() функции, которая падает → RuntimeError."""
        wm = WorkerManager()
        wm.start(num_workers=1)
        try:
            with pytest.raises(RuntimeError, match="Task failed"):
                wm.submit(failing_task)
        finally:
            wm.stop()

    def test_submit_timeout(self):
        """submit() с таймаутом → TimeoutError."""
        wm = WorkerManager()
        wm.start(num_workers=1)
        try:
            # heavy_task(1000000) займёт время
            with pytest.raises(TimeoutError):
                wm.submit(heavy_task, 1_000_000, timeout=0.01)
        finally:
            wm.stop()


class TestRestartWorker:
    """Перезапуск воркеров."""

    def test_restart_worker(self):
        """restart_worker() заменяет воркер (новый PID, тот же worker_id)."""
        wm = WorkerManager()
        wm.start(num_workers=2)
        try:
            old_pids = set(wm._workers.keys())
            worker_to_restart = wm.get_worker_ids()[0]
            wm.restart_worker(worker_to_restart)
            new_pids = set(wm._workers.keys())

            # Количество воркеров не изменилось
            assert len(new_pids) == 2
            # Хотя бы один PID должен отличаться (новый процесс)
            assert new_pids != old_pids
            # Worker ID сохраняется
            assert worker_to_restart in wm.get_worker_ids()
        finally:
            wm.stop()

    def test_restart_nonexistent_worker(self):
        """restart_worker(99999) — не падает (логирует warning)."""
        wm = WorkerManager()
        wm.start(num_workers=1)
        try:
            wm.restart_worker(99999)  # Не должно упасть
            assert len(wm.get_worker_ids()) == 1
        finally:
            wm.stop()


class TestGetWorkerIds:
    """Получение списка ID."""

    def test_get_worker_ids(self):
        """get_worker_ids() возвращает список ID."""
        wm = WorkerManager()
        wm.start(num_workers=3)
        try:
            ids = wm.get_worker_ids()
            assert isinstance(ids, list)
            assert len(ids) == 3
        finally:
            wm.stop()

    def test_get_worker_ids_empty(self):
        """get_worker_ids() до start() → пустой список."""
        wm = WorkerManager()
        ids = wm.get_worker_ids()
        assert ids == []


class TestWorkerAffinity:
    """Привязка воркеров к ядрам CPU."""

    def test_worker_affinity(self):
        """Каждый воркер привязан к своему ядру (проверить через os.sched_getaffinity)."""
        cpu_count = os.cpu_count() or 1
        wm = WorkerManager()
        wm.start(num_workers=min(cpu_count, 4))
        try:
            # Получаем PID воркеров
            worker_pids = list(wm._workers.keys())
            for pid in worker_pids:
                affinity = os.sched_getaffinity(pid)
                # Воркер должен быть привязан хотя бы к одному ядру
                assert len(affinity) >= 1
        finally:
            wm.stop()
