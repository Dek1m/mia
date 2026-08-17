"""WorkerManager — управление lifecycle воркеров."""
from __future__ import annotations

import multiprocessing
import os
import queue
import signal
import sys
import threading
import time
from typing import Any, Callable
from uuid import uuid4

from argenta_logging import get_logger
from core.shared_memory import SharedMemory
from monitoring.metrics import (
    worker_manager_active,
    worker_manager_restarts_total,
    worker_manager_tasks_submitted_total,
    worker_manager_task_duration_seconds,
    processpool_spawned_total,
    processpool_killed_total,
)
from pools.load_balancer import LoadBalancer, WorkerState
from pools.worker_thread_pool import WorkerThreadPool

log = get_logger(__name__)


def _worker_entry(
    task_queue: multiprocessing.Queue,
    result_queue: multiprocessing.Queue,
    worker_id: int,
    core_id: int,
    heartbeat_queue: multiprocessing.Queue | None = None,
    max_threads: int = 4,
) -> None:
    """Точка входа worker-процесса.

    Создаёт WorkerThreadPool внутри процесса и выполняет задачи через него.
    """
    signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))

    # Привязка к ядру
    try:
        os.sched_setaffinity(0, {core_id})
    except (OSError, AttributeError):
        pass

    log.info("Worker started", extra={"worker_id": worker_id, "pid": os.getpid(), "core": core_id})

    # Создаём ThreadPool внутри воркера
    thread_pool = WorkerThreadPool(max_threads=max_threads)
    thread_pool.start()

    last_heartbeat = time.time()

    try:
        while True:
            try:
                task = task_queue.get(timeout=1.0)
                if task is None:
                    break

                request_id, fn, args, kwargs = task
                try:
                    # Выполняем через WorkerThreadPool
                    future = thread_pool.submit(fn, *args, **kwargs)
                    result = future.result()
                    result_queue.put((request_id, "ok", result))
                except Exception as e:
                    log.error("Worker error", extra={"worker_id": worker_id, "error": str(e)})
                    result_queue.put((request_id, "error", str(e)))
            except queue.Empty:
                pass

            # Каждые N секунд отправляем heartbeat
            now = time.time()
            from core.config import MiaConfig
            heartbeat_period = MiaConfig.get().get_value("pools.worker.heartbeat_period", 5.0)
            if heartbeat_queue is not None and now - last_heartbeat >= heartbeat_period:
                try:
                    heartbeat_queue.put(os.getpid())
                    last_heartbeat = now
                except (OSError, ValueError):
                    pass
    finally:
        thread_pool.shutdown(wait=False)
        log.info("Worker thread pool shut down", extra={"worker_id": worker_id})

    log.info("Worker stopped", extra={"worker_id": worker_id})


class WorkerManager:
    """Управление lifecycle воркеров: spawn, restart, shutdown.

    Каждый воркер привязан к ядру через CPU affinity.
    Внутри каждого воркера создаётся WorkerThreadPool.
    Интегрирован с LoadBalancer для выбора наименее загруженного.
    """

    def __init__(
        self,
        load_balancer: LoadBalancer | None = None,
        heartbeat_monitor: Any | None = None,
        shared_memory: SharedMemory | None = None,
    ) -> None:
        self._load_balancer = load_balancer or LoadBalancer()
        self._heartbeat_monitor = heartbeat_monitor
        self._shared_memory = shared_memory or SharedMemory()
        self._workers: dict[int, multiprocessing.Process] = {}
        self._worker_ids: dict[int, int] = {}
        self._worker_states: dict[int, WorkerState] = {}
        self._task_queue: multiprocessing.Queue | None = None
        self._result_queue: multiprocessing.Queue | None = None
        self._heartbeat_queue: multiprocessing.Queue | None = None
        self._lock = threading.Lock()
        self._pending: dict[str, tuple[threading.Event, list]] = {}
        self._reader_thread: threading.Thread | None = None
        self._reader_running = False
        self._heartbeat_thread: threading.Thread | None = None
        self._cpu_count = os.cpu_count() or 1

        # Конфигурация ThreadPool внутри воркеров
        from core.config import MiaConfig
        cfg = MiaConfig.get()
        self._max_threads_per_worker = cfg.get_value("pools.worker.thread_pool.max_threads", 4)

    @property
    def load_balancer(self) -> LoadBalancer:
        return self._load_balancer

    @property
    def shared_memory(self) -> SharedMemory:
        return self._shared_memory

    def start(self, num_workers: int | None = None) -> None:
        """Запустить N воркеров (по числу ядер по умолчанию).

        Args:
            num_workers: Количество воркеров. None = число ядер.
        """
        n = num_workers or self._cpu_count
        self._task_queue = multiprocessing.Queue()
        self._result_queue = multiprocessing.Queue()
        self._heartbeat_queue = multiprocessing.Queue()

        self._reader_running = True
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()

        for i in range(n):
            self._spawn_worker(i)

        worker_manager_active.set(n)
        log.info("WorkerManager started", extra={"num_workers": n})

    def stop(self, timeout: float | None = None) -> None:
        """Graceful shutdown всех воркеров.

        Args:
            timeout: Таймаут ожидания завершения (None → config).
        """
        if self._task_queue is None:
            return
        if timeout is None:
            from core.config import MiaConfig
            timeout = MiaConfig.get().get_value("pools.worker.stop_timeout", 5.0)

        for _ in self._workers.values():
            self._task_queue.put(None)

        for p in self._workers.values():
            p.join(timeout=timeout)
            if p.is_alive():
                p.terminate()
                processpool_killed_total.inc()

        self._reader_running = False
        if self._reader_thread:
            self._reader_thread.join(timeout=2)

        if self._heartbeat_thread:
            self._heartbeat_thread.join(timeout=2)

        if self._heartbeat_monitor:
            for pid in list(self._worker_ids.keys()):
                self._heartbeat_monitor.unregister(pid)

        self._workers.clear()
        self._worker_ids.clear()
        self._worker_states.clear()
        self._task_queue = None
        self._result_queue = None
        # Закрываем heartbeat queue стандартным образом — feeder-поток не должен висеть
        if self._heartbeat_queue is not None:
            self._heartbeat_queue.close()
            self._heartbeat_queue.join_thread()
            self._heartbeat_queue = None
        worker_manager_active.set(0)
        log.info("WorkerManager stopped")

    def restart_worker(self, worker_id: int) -> None:
        """Убить и перезапустить воркер.

        Args:
            worker_id: ID воркера для перезапуска.
        """
        with self._lock:
            # Найти pid по worker_id
            dead_pid = None
            for pid, wid in self._worker_ids.items():
                if wid == worker_id:
                    dead_pid = pid
                    break

            if dead_pid is None:
                log.warning("Worker not found for restart", extra={"worker_id": worker_id})
                return

            p = self._workers.pop(dead_pid, None)
            self._worker_ids.pop(dead_pid, None)
            self._worker_states.pop(worker_id, None)

            if self._heartbeat_monitor:
                self._heartbeat_monitor.unregister(dead_pid)

            if p and p.is_alive():
                p.terminate()
                p.join(timeout=2)

            processpool_killed_total.inc()
            log.warning("Worker killed, restarting", extra={"worker_id": worker_id, "dead_pid": dead_pid})

            self._spawn_worker(worker_id)
            worker_manager_restarts_total.inc()
            log.info("Worker restarted", extra={"worker_id": worker_id})

    def get_worker_ids(self) -> list[int]:
        """Получить список ID активных воркеров."""
        with self._lock:
            return list(self._worker_ids.values())

    def submit(self, fn: Callable, *args: Any, timeout: float | None = None, **kwargs: Any) -> Any:
        """Отправить задачу через LoadBalancer.

        Args:
            fn: Функция для выполнения.
            *args: Позиционные аргументы.
            timeout: Максимальное время ожидания.
            **kwargs: Именованные аргументы.

        Returns:
            Результат выполнения.
        """
        if self._task_queue is None:
            raise RuntimeError("WorkerManager not started. Call start() first.")

        # Обновить состояния воркеров для балансировщика
        self._sync_worker_states()

        request_id = uuid4().hex
        result_event = threading.Event()
        result_container = [None, None]

        with self._lock:
            self._pending[request_id] = (result_event, result_container)

        log.info("Task submitted", extra={"request_id": request_id, "timeout": timeout})
        start_time = time.monotonic()

        try:
            self._task_queue.put((request_id, fn, args, kwargs))
        except (OSError, ValueError):
            with self._lock:
                self._pending.pop(request_id, None)
            worker_manager_tasks_submitted_total.labels(status="error").inc()
            log.error("Task submit failed", extra={"request_id": request_id, "reason": "queue_closed"})
            raise RuntimeError("WorkerManager is shut down")

        if not result_event.wait(timeout=timeout):
            with self._lock:
                self._pending.pop(request_id, None)
            duration = time.monotonic() - start_time
            worker_manager_task_duration_seconds.observe(duration)
            worker_manager_tasks_submitted_total.labels(status="timeout").inc()
            log.warning("Task timed out", extra={"request_id": request_id, "timeout": timeout, "duration": duration})
            raise TimeoutError(f"Task timed out after {timeout}s")

        status, result = result_container
        duration = time.monotonic() - start_time
        worker_manager_task_duration_seconds.observe(duration)

        if status == "error":
            worker_manager_tasks_submitted_total.labels(status="error").inc()
            log.error("Task failed", extra={"request_id": request_id, "duration": duration, "error": str(result)[:200]})
            raise RuntimeError(f"Task failed: {result}")

        worker_manager_tasks_submitted_total.labels(status="ok").inc()
        log.info("Task completed", extra={"request_id": request_id, "duration": duration})
        return result

    def _spawn_worker(self, worker_id: int) -> None:
        """Запустить один worker-процесс."""
        core_id = worker_id % self._cpu_count
        p = multiprocessing.Process(
            target=_worker_entry,
            args=(
                self._task_queue, self._result_queue, worker_id, core_id,
                self._heartbeat_queue, self._max_threads_per_worker,
            ),
        )
        p.start()
        self._workers[p.pid] = p
        self._worker_ids[p.pid] = worker_id
        processpool_spawned_total.inc()

        state = WorkerState(worker_id=worker_id, pid=p.pid, core_id=core_id)
        self._worker_states[worker_id] = state
        self._load_balancer.update_worker_state(worker_id, state)

        if self._heartbeat_monitor:
            self._heartbeat_monitor.register(p.pid, {"worker_id": worker_id})

        log.info("Worker spawned", extra={"worker_id": worker_id, "pid": p.pid, "core": core_id})

    def _reader_loop(self) -> None:
        """Читает результаты из result_queue."""
        while self._reader_running:
            try:
                request_id, status, result = self._result_queue.get(timeout=1.0)
            except (queue.Empty, OSError):
                continue

            with self._lock:
                entry = self._pending.pop(request_id, None)

            if entry:
                event, container = entry
                container[0] = status
                container[1] = result
                event.set()

    def _heartbeat_loop(self) -> None:
        """Читает heartbeat из очереди и обновляет HeartbeatMonitor."""
        while self._reader_running:
            try:
                pid = self._heartbeat_queue.get(timeout=1.0)
            except (queue.Empty, OSError, AttributeError):
                # AttributeError если очередь уже None при гонке с stop()
                continue

            if self._heartbeat_monitor:
                try:
                    self._heartbeat_monitor.update(pid)
                except Exception:
                    pass

    def _sync_worker_states(self) -> None:
        """Синхронизировать состояния воркеров."""
        with self._lock:
            for worker_id, state in self._worker_states.items():
                state.active_tasks = max(0, state.active_tasks)
                self._load_balancer.update_worker_state(worker_id, state)
