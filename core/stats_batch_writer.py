"""StatsBatchWriter — батчевая запись статистики задач в PostgreSQL."""
from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from argenta_logging import get_logger
from core.task import Task

log = get_logger(__name__)

# Прямой импорт метрик
try:
    from monitoring.metrics import task_store_flush_total as _flush_counter
except ImportError:
    _flush_counter = None


class StatsBatchWriter:
    """Батчевый писатель статистики задач.

    Накапливает задачи в буфере и периодически записывает батчем
    в task_history + task_stats.

    Thread-safe: ``add()`` можно вызывать из любого потока.

    Args:
        db: Database facade (IDatabase).
        batch_size: Максимальный размер буфера перед flush.
        flush_interval: Интервал автоматического flush (секунды).
    """

    def __init__(
        self,
        db: Any,
        batch_size: int = 500,
        flush_interval: float = 5.0,
    ) -> None:
        self._db = db
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._buffer: list[dict] = []
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._flush_event = threading.Event()

    def add(self, task: Task) -> None:
        """Добавить задачу в буфер для записи."""
        now = datetime.now(timezone.utc)
        mono_ref = task.created_at

        def _to_dt(mono: float | None) -> datetime | None:
            if mono is None:
                return None
            return now + timedelta(seconds=mono - mono_ref)

        row = {
            "task_id": str(task.id),
            "module_id": task.module_id,
            "task_type": task.task_type.value,
            "fn_name": task.fn_name,
            "status": task.status.value,
            "started_at": _to_dt(task.started_at),
            "completed_at": _to_dt(task.completed_at),
            "duration_ms": task.duration * 1000 if task.duration is not None else None,
            "error": task.error,
            "metadata": task.metadata,
            "created_at": now,
        }
        with self._lock:
            self._buffer.append(row)
            if len(self._buffer) >= self._batch_size:
                self._flush_event.set()

    async def flush(self) -> None:
        """Batch INSERT в task_history + UPDATE task_stats."""
        with self._lock:
            if not self._buffer:
                return
            batch = self._buffer[:]
            self._buffer.clear()

        if not batch:
            return

        try:
            await self._flush_history(batch)
            await self._flush_stats(batch)
            if _flush_counter is not None:
                _flush_counter.inc()
            log.debug("StatsBatchWriter flush completed", extra={"batch_size": len(batch)})
        except Exception as e:
            log.error(
                "StatsBatchWriter flush failed",
                extra={"error": str(e), "batch_size": len(batch)},
            )

    async def _flush_history(self, batch: list[dict]) -> None:
        """Batch INSERT в task_history."""
        columns = [
            "task_id", "module_id", "task_type", "fn_name", "status",
            "started_at", "completed_at", "duration_ms", "error", "metadata", "created_at",
        ]

        placeholders = []
        params: list[Any] = []
        idx = 1
        for row in batch:
            row_ph = []
            for col in columns:
                row_ph.append(f"${idx}")
                params.append(row[col])
                idx += 1
            placeholders.append(f"({', '.join(row_ph)})")

        query = (
            f"INSERT INTO task_history ({', '.join(columns)}) "
            f"VALUES {', '.join(placeholders)}"
        )

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._db.execute, query, *params)

    async def _flush_stats(self, batch: list[dict]) -> None:
        """UPDATE task_stats для каждой уникальной пары (module_id, task_type)."""
        groups: dict[tuple[str, str], list[float]] = {}
        counts: dict[tuple[str, str], int] = {}
        for row in batch:
            key = (row["module_id"], row["task_type"])
            counts[key] = counts.get(key, 0) + 1
            dur = row["duration_ms"]
            if dur is not None:
                groups.setdefault(key, []).append(dur)

        loop = asyncio.get_running_loop()

        # Группы с duration — обновляем count, avg, p95
        for (module_id, task_type), durations in groups.items():
            count = counts[(module_id, task_type)]
            avg = sum(durations) / len(durations)
            p95 = _percentile(durations, 0.95)

            query = """
                INSERT INTO task_stats (module_id, task_type, count, avg_duration_ms, p95_duration_ms, last_updated)
                VALUES ($1, $2, $3, $4, $5, NOW())
                ON CONFLICT (module_id, task_type) DO UPDATE SET
                    count = task_stats.count + EXCLUDED.count,
                    avg_duration_ms = CASE
                        WHEN task_stats.count = 0 THEN EXCLUDED.avg_duration_ms
                        ELSE (task_stats.avg_duration_ms * task_stats.count + EXCLUDED.avg_duration_ms * EXCLUDED.count)
                             / (task_stats.count + EXCLUDED.count)
                    END,
                    p95_duration_ms = EXCLUDED.p95_duration_ms,
                    last_updated = NOW()
            """
            await loop.run_in_executor(
                None, self._db.execute, query, module_id, task_type, count, avg, p95
            )

        # Группы без duration — обновляем только count
        for (module_id, task_type), count in counts.items():
            if (module_id, task_type) not in groups:
                query = """
                    INSERT INTO task_stats (module_id, task_type, count, last_updated)
                    VALUES ($1, $2, $3, NOW())
                    ON CONFLICT (module_id, task_type) DO UPDATE SET
                        count = task_stats.count + EXCLUDED.count,
                        last_updated = NOW()
                """
                await loop.run_in_executor(
                    None, self._db.execute, query, module_id, task_type, count
                )

    def start(self) -> None:
        """Запуск фонового потока для periodic flush."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_flusher,
            daemon=True,
            name="stats-batch-writer",
        )
        self._thread.start()
        log.info(
            "StatsBatchWriter started",
            extra={"batch_size": self._batch_size, "flush_interval": self._flush_interval},
        )

    def _run_flusher(self) -> None:
        """Фоновый поток: периодически вызывает flush()."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            while self._running:
                self._flush_event.wait(timeout=self._flush_interval)
                self._flush_event.clear()
                if not self._running:
                    break
                self._do_flush()
            # Финальный flush при остановке
            self._do_flush()
        finally:
            self._loop.close()
            self._loop = None

    def _do_flush(self) -> None:
        """Запуск async flush из синхронного контекста."""
        if self._loop is None or self._loop.is_closed():
            return
        try:
            self._loop.run_until_complete(self.flush())
        except Exception as e:
            log.error("StatsBatchWriter flush error", extra={"error": str(e)})

    def stop(self) -> None:
        """Корректная остановка: финальный flush + остановка потока."""
        self._running = False
        self._flush_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=10.0)
        self._loop = None
        log.info("StatsBatchWriter stopped", extra={"remaining_buffer": self.buffer_size()})

    def buffer_size(self) -> int:
        """Текущий размер буфера."""
        with self._lock:
            return len(self._buffer)


def _percentile(data: list[float], pct: float) -> float:
    """Вычислить перцентиль из списка значений."""
    if not data:
        return 0.0
    sorted_data = sorted(data)
    idx = int(len(sorted_data) * pct)
    return sorted_data[min(idx, len(sorted_data) - 1)]
