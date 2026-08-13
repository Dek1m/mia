"""CpuMetricsCollector — сбор метрик CPU через /proc/stat (Linux only)."""
from __future__ import annotations

import os
import threading
import time
from argenta_logging import get_logger
from monitoring.metrics import cpu_load_gauge, cpu_per_core_load_gauge

log = get_logger(__name__)


class CpuMetricsCollector:
    """Сбор метрик CPU: общая нагрузка и по ядрам.

    Читает /proc/stat для точных delta jiffies.
    Thread-safe через RLock.
    """

    def __init__(self, collect_interval: float = 1.0) -> None:
        self._collect_interval = collect_interval
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._running = False
        self._cpu_count = os.cpu_count() or 1

        self._prev_total: float = 0.0
        self._prev_idle: float = 0.0
        self._prev_per_core: list[tuple[float, float]] = []

        self._cpu_load: float = 0.0
        self._per_core_load: list[float] = [0.0] * self._cpu_count

    def get_cpu_load(self) -> float:
        with self._lock:
            return self._cpu_load

    def get_per_core_load(self) -> list[float]:
        with self._lock:
            return list(self._per_core_load)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._collect_loop, daemon=True)
        self._thread.start()
        log.info("CpuMetricsCollector started", extra={"interval": self._collect_interval})

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
        log.info("CpuMetricsCollector stopped")

    def _collect_loop(self) -> None:
        while self._running:
            self._collect()
            time.sleep(self._collect_interval)

    def _collect(self) -> None:
        try:
            with open("/proc/stat") as f:
                lines = f.readlines()
        except OSError as e:
            log.error("Failed to read /proc/stat", extra={"error": str(e)})
            return

        cpu_line = lines[0].split()
        values = [float(x) for x in cpu_line[1:]]
        total = sum(values)
        idle = values[3] if len(values) > 3 else 0.0

        with self._lock:
            if self._prev_total > 0:
                delta_total = total - self._prev_total
                delta_idle = idle - self._prev_idle
                if delta_total > 0:
                    self._cpu_load = max(0.0, min(1.0, 1.0 - delta_idle / delta_total))
                cpu_load_gauge.set(self._cpu_load)
            self._prev_total = total
            self._prev_idle = idle

        per_core: list[float] = []
        for i in range(self._cpu_count):
            if i + 1 < len(lines):
                core_line = lines[i + 1].split()
                core_values = [float(x) for x in core_line[1:]]
                core_total = sum(core_values)
                core_idle = core_values[3] if len(core_values) > 3 else 0.0
                per_core.append((core_total, core_idle))

        with self._lock:
            new_per_core_load: list[float] = []
            for idx, (core_total, core_idle) in enumerate(per_core):
                if idx < len(self._prev_per_core):
                    prev_total, prev_idle = self._prev_per_core[idx]
                    delta_total = core_total - prev_total
                    delta_idle = core_idle - prev_idle
                    if delta_total > 0:
                        load = max(0.0, min(1.0, 1.0 - delta_idle / delta_total))
                    else:
                        load = 0.0
                else:
                    load = 0.0
                new_per_core_load.append(load)
                cpu_per_core_load_gauge.labels(core=str(idx)).set(load)

            self._per_core_load = new_per_core_load
            self._prev_per_core = per_core
