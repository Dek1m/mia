"""Unit-тесты для CpuMetricsCollector — сбор метрик CPU."""
import os
import time

import pytest

from pools.cpu_metrics import CpuMetricsCollector


@pytest.fixture
def collector():
    """Создаёт CpuMetricsCollector для тестов."""
    c = CpuMetricsCollector(collect_interval=0.5)
    yield c
    # Гарантированная остановка после теста
    try:
        c.stop()
    except Exception:
        pass


# === Тесты создания ===

def test_cpu_metrics_creation():
    """CpuMetricsCollector() создаётся без ошибок."""
    c = CpuMetricsCollector()
    assert c is not None
    assert isinstance(c, CpuMetricsCollector)


def test_cpu_metrics_custom_interval():
    """CpuMetricsCollector(collect_interval=0.5) — кастомный интервал."""
    c = CpuMetricsCollector(collect_interval=0.5)
    assert c._collect_interval == 0.5


# === Тесты get_cpu_load ===

def test_get_cpu_load(collector):
    """get_cpu_load() возвращает float от 0.0 до 1.0."""
    # Первая итерация может вернуть 0.0 (нет предыдущих значений)
    # Запускаем коллекцию и ждём хотя бы одну дельту
    collector.start()
    time.sleep(1.5)  # Ждём >= 2 коллекций для дельты
    load = collector.get_cpu_load()
    assert isinstance(load, float)
    assert 0.0 <= load <= 1.0


# === Тесты get_per_core_load ===

def test_get_per_core_load(collector):
    """get_per_core_load() возвращает list длиной cpu_count()."""
    cpu_count = os.cpu_count() or 1
    collector.start()
    time.sleep(1.5)
    per_core = collector.get_per_core_load()
    assert isinstance(per_core, list)
    assert len(per_core) == cpu_count
    assert all(isinstance(v, float) for v in per_core)
    assert all(0.0 <= v <= 1.0 for v in per_core)


# === Тесты start/stop ===

def test_start_stop(collector):
    """start/stop работают без ошибок."""
    collector.start()
    assert collector._running is True
    assert collector._thread is not None
    assert collector._thread.is_alive()

    collector.stop()
    assert collector._running is False
    assert not collector._thread.is_alive()


def test_start_is_idempotent(collector):
    """Повторный start() не создаёт второй поток."""
    collector.start()
    thread1 = collector._thread
    collector.start()  # Не должно создать новый поток
    assert collector._thread is thread1
    collector.stop()


# === Тесты чтения /proc/stat ===

def test_proc_stat_reading():
    """Проверить что /proc/stat читается (Linux only)."""
    if not os.path.exists("/proc/stat"):
        pytest.skip("No /proc/stat — not Linux")
    with open("/proc/stat") as f:
        lines = f.readlines()
    assert len(lines) > 0
    # Первая строка — общая статистика CPU
    assert lines[0].startswith("cpu")
