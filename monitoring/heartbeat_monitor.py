"""HeartbeatMonitor — мониторинг живых процессов."""
from __future__ import annotations
import threading
import time
from typing import Callable
from argenta_logging import get_logger
from monitoring.metrics import heartbeat_missed_total

log = get_logger(__name__)

class HeartbeatMonitor:
    """Мониторинг heartbeat от процессов.

    Args:
        timeout: Таймаут в секундах без heartbeat (по умолчанию 30).
        check_interval: Интервал проверки в секундах (по умолчанию 5).
    """

    def __init__(self, timeout: float | None = None, check_interval: float | None = None) -> None:
        if timeout is None or check_interval is None:
            from core.config import MiaConfig
            cfg = MiaConfig.get()
            if timeout is None:
                timeout = cfg.get_value("monitoring.heartbeat.timeout", 30.0)
            if check_interval is None:
                check_interval = cfg.get_value("monitoring.heartbeat.check_interval", 5.0)
        self._timeout = timeout
        self._check_interval = check_interval
        self._heartbeats: dict[int, float] = {}  # pid -> last_heartbeat_time
        self._meta: dict[int, dict] = {}  # pid -> мета (worker_id и пр.)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._running = False
        self._on_timeout: Callable[[int], None] | None = None
        log.info("HeartbeatMonitor created", extra={"timeout": timeout, "check_interval": check_interval})

    def set_timeout_handler(self, handler: Callable[[int], None]) -> None:
        """Установить обработчик таймаута (вызывается при смерти процесса)."""
        self._on_timeout = handler

    def register(self, pid: int, meta: dict | None = None) -> None:
        """Зарегистрировать процесс для мониторинга.

        Args:
            pid: PID процесса.
            meta: Произвольные мета-данные (worker_id и пр.), попадут в логи.
        """
        with self._lock:
            self._heartbeats[pid] = time.time()
            if meta:
                self._meta[pid] = meta
            log.debug("Process registered", extra={"pid": pid, **(meta or {})})

    def unregister(self, pid: int) -> None:
        """Убрать процесс из мониторинга."""
        with self._lock:
            self._heartbeats.pop(pid, None)
            self._meta.pop(pid, None)
            log.debug("Process unregistered", extra={"pid": pid})

    def update(self, pid: int) -> None:
        """Обновить heartbeat для процесса."""
        with self._lock:
            if pid in self._heartbeats:
                self._heartbeats[pid] = time.time()

    def start(self) -> None:
        """Запустить мониторинг в отдельном потоке."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        log.info("HeartbeatMonitor started")

    def stop(self) -> None:
        """Остановить мониторинг."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5.0)
        log.info("HeartbeatMonitor stopped")

    def _monitor_loop(self) -> None:
        """Основной цикл мониторинга."""
        while self._running:
            time.sleep(self._check_interval)
            self._check_heartbeats()

    def _check_heartbeats(self) -> None:
        """Проверить heartbeat всех процессов."""
        now = time.time()
        dead_pids = []

        with self._lock:
            for pid, last_beat in self._heartbeats.items():
                if now - last_beat > self._timeout:
                    dead_pids.append(pid)
                    heartbeat_missed_total.inc()
                    log.warning(
                        "Heartbeat timeout",
                        extra={"pid": pid, "last_beat": now - last_beat, **self._meta.get(pid, {})},
                    )

        # Вызов обработчика вне лока
        for pid in dead_pids:
            if self._on_timeout:
                try:
                    self._on_timeout(pid)
                except Exception as e:
                    log.error("Heartbeat timeout handler error", extra={"pid": pid, "error": str(e)})

    @property
    def active_count(self) -> int:
        """Количество отслеживаемых процессов."""
        with self._lock:
            return len(self._heartbeats)
