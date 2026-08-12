"""ProcessPool — пул процессов с affinity и fault tolerance."""
from __future__ import annotations
import multiprocessing
import os
import queue
import signal
import sys
import threading
import time
import uuid
from typing import Any, Callable
from argenta_logging import get_logger
from metrics import processpool_active, processpool_spawned_total, processpool_killed_total

log = get_logger(__name__)

def _worker_entry(task_queue: multiprocessing.Queue, result_queue: multiprocessing.Queue,
                   worker_id: int, affinity_provider: "CpuAffinityProvider" | None) -> None:
    """Точка входа worker-процесса."""
    # Установить обработчик SIGTERM
    signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))

    # Привязать к ядру
    if affinity_provider:
        core_id = worker_id % affinity_provider.get_cpu_count()
        affinity_provider.set_affinity(0, {core_id})

    log.info("Worker started", extra={"worker_id": worker_id, "pid": os.getpid()})

    while True:
        try:
            task = task_queue.get(timeout=1.0)
            if task is None:  # Sentinel для остановки
                break
            request_id, fn, args, kwargs = task
            try:
                result = fn(*args, **kwargs)
                result_queue.put((request_id, "ok", result))
            except Exception as e:
                log.error("Worker error", extra={"worker_id": worker_id, "error": str(e)})
                result_queue.put((request_id, "error", str(e)))
        except queue.Empty:
            continue

    log.info("Worker stopped", extra={"worker_id": worker_id})


class ProcessPool:
    """Пул процессов с CPU affinity и fault tolerance.

    Args:
        num_processes: Количество процессов.
        affinity_provider: Провайдер CPU affinity (опционально).
        heartbeat_monitor: Монитор heartbeat (опционально).
    """

    def __init__(self, num_processes: int | None = None,
                 affinity_provider: "CpuAffinityProvider" | None = None,
                 heartbeat_monitor: "HeartbeatMonitor" | None = None) -> None:
        self._num_processes = num_processes or os.cpu_count() or 1
        self._affinity = affinity_provider
        self._heartbeat_monitor = heartbeat_monitor
        self._workers: dict[int, multiprocessing.Process] = {}  # pid -> process
        self._worker_ids: dict[int, int] = {}  # pid -> worker_id
        self._task_queue: multiprocessing.Queue | None = None
        self._result_queue: multiprocessing.Queue | None = None
        self._restart_enabled = heartbeat_monitor is not None
        self._lock = threading.Lock()
        self._pending: dict[str, tuple[threading.Event, list]] = {}
        self._reader_thread: threading.Thread | None = None
        self._reader_running = False
        log.info("ProcessPool created", extra={"num_processes": self._num_processes})

    def start(self) -> None:
        """Запустить пул процессов."""
        self._task_queue = multiprocessing.Queue()
        self._result_queue = multiprocessing.Queue()

        # Запуск reader-потока для маршрутизации результатов
        self._reader_running = True
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

        for i in range(self._num_processes):
            self._spawn_worker(i)

        log.info("ProcessPool started", extra={"num_processes": self._num_processes})

    def _reader_loop(self) -> None:
        """Читает результаты из result_queue и маршрутизирует по request_id."""
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

    def _spawn_worker(self, worker_id: int) -> None:
        """Запустить один worker-процесс."""
        p = multiprocessing.Process(
            target=_worker_entry,
            args=(self._task_queue, self._result_queue, worker_id, self._affinity),
        )
        p.start()
        self._workers[p.pid] = p
        self._worker_ids[p.pid] = worker_id
        processpool_active.inc()
        processpool_spawned_total.inc()

        # Зарегистрировать в heartbeat monitor
        if self._heartbeat_monitor:
            self._heartbeat_monitor.register(p.pid)

        log.info("Worker spawned", extra={"worker_id": worker_id, "pid": p.pid})

    def submit(self, fn: Callable, *args, timeout: float | None = None, **kwargs) -> Any:
        """Отправить задачу и дождаться результата.

        Args:
            fn: Функция для выполнения.
            *args: Позиционные аргументы.
            timeout: Максимальное время ожидания (сек). None — бесконечно.
            **kwargs: Именованные аргументы.

        Returns:
            Результат выполнения.

        Raises:
            TimeoutError: Если превышен timeout.
        """
        if self._task_queue is None:
            raise RuntimeError("ProcessPool not started. Call start() first.")

        request_id = uuid.uuid4().hex
        result_event = threading.Event()
        result_container = [None, None]  # [status, result]

        with self._lock:
            self._pending[request_id] = (result_event, result_container)

        try:
            self._task_queue.put((request_id, fn, args, kwargs))
        except (OSError, ValueError):
            with self._lock:
                self._pending.pop(request_id, None)
            raise RuntimeError("ProcessPool is shut down")

        # Обновить heartbeat при отправке задачи
        self._update_heartbeat_for_active_workers()

        if not result_event.wait(timeout=timeout):
            with self._lock:
                self._pending.pop(request_id, None)
            raise TimeoutError(f"Task timed out after {timeout}s")

        status, result = result_container

        # Обновить heartbeat после получения результата
        self._update_heartbeat_for_active_workers()

        if status == "error":
            raise RuntimeError(f"Task failed: {result}")
        return result

    def _update_heartbeat_for_active_workers(self) -> None:
        """Обновить heartbeat для всех живых worker'ов."""
        if not self._heartbeat_monitor:
            return
        for pid in list(self._workers.keys()):
            if self._workers[pid].is_alive():
                self._heartbeat_monitor.update(pid)

    def _restart_worker(self, dead_pid: int) -> None:
        """Перезапустить упавший worker."""
        if not self._restart_enabled:
            return

        with self._lock:
            worker_id = self._worker_ids.get(dead_pid, 0)

            # Убрать мёртвого воркера
            self._workers.pop(dead_pid, None)
            self._worker_ids.pop(dead_pid, None)
            self._heartbeat_monitor.unregister(dead_pid)

            processpool_active.dec()
            processpool_killed_total.inc()

            log.warning("Worker died, restarting", extra={"dead_pid": dead_pid, "worker_id": worker_id})

            # Запустить нового воркера
            self._spawn_worker(worker_id)

            # Публикация события happen AFTER restart
            # Импорт здесь чтобы избежать циклических импортов
            from event_bus import EVENT_PROCESS_RESTARTED
            log.info("Worker restarted", extra={"worker_id": worker_id})

    def shutdown(self, timeout: float = 5.0) -> None:
        """Остановить пул процессов.

        Args:
            timeout: Таймаут ожидания завершения.
        """
        if self._task_queue is None:
            return

        # Остановить heartbeat monitor
        if self._heartbeat_monitor:
            self._heartbeat_monitor.stop()

        # Отправить sentinel для каждого воркера
        for _ in self._workers.values():
            self._task_queue.put(None)

        # Ждать завершения
        for p in self._workers.values():
            p.join(timeout=timeout)
            if p.is_alive():
                p.terminate()
                processpool_killed_total.inc()

        # Остановить reader-поток
        self._reader_running = False
        if self._reader_thread:
            self._reader_thread.join(timeout=2)

        self._workers.clear()
        self._worker_ids.clear()
        self._task_queue = None
        self._result_queue = None
        processpool_active.set(0)
        log.info("ProcessPool stopped")
