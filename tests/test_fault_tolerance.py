"""Тесты fault tolerance для ProcessPool."""
import multiprocessing
import os
import signal
import time

import pytest

from heartbeat_monitor import HeartbeatMonitor
from process_pool import ProcessPool
from metrics import processpool_active, processpool_killed_total, heartbeat_missed_total


# === Топ-левел функции (для сериализации через multiprocessing) ===

def _noop():
    return None


def _identity(x):
    return x


def _sleep_forever():
    """Зависает навечно — для тестов убийства."""
    while True:
        time.sleep(0.1)


def _return_pid():
    return os.getpid()


# === Тесты restart ===

def test_restart_worker():
    """Убить worker → проверить перезапуск нового worker."""
    heartbeat = HeartbeatMonitor(timeout=2.0, check_interval=0.1)
    pp = ProcessPool(num_processes=1, heartbeat_monitor=heartbeat)
    pp.start()

    # Запоминаем первого worker'а
    original_pids = list(pp._workers.keys())
    assert len(original_pids) == 1
    original_pid = original_pids[0]

    # Убиваем worker через SIGKILL
    os.kill(original_pid, signal.SIGKILL)
    time.sleep(0.2)

    # Вызываем restart вручную (в нормальной эксплуатации heartbeat это делает)
    pp._restart_worker(original_pid)

    # Должен появиться новый worker с другим PID
    new_pids = list(pp._workers.keys())
    assert len(new_pids) == 1
    new_pid = new_pids[0]
    assert new_pid != original_pid

    # Новый worker должен быть жив
    assert pp._workers[new_pid].is_alive()

    pp.shutdown(timeout=3)


def test_heartbeat_triggers_restart():
    """Heartbeat timeout → restart мёртвого worker'а."""
    heartbeat = HeartbeatMonitor(timeout=0.2, check_interval=0.1)
    pp = ProcessPool(num_processes=1, heartbeat_monitor=heartbeat)
    pp.start()

    original_pid = list(pp._workers.keys())[0]

    # Устанавливаем обработчик таймаута — вызывает _restart_worker
    heartbeat.set_timeout_handler(pp._restart_worker)
    heartbeat.start()

    # Убиваем worker
    os.kill(original_pid, signal.SIGKILL)

    # Ждём пока heartbeat обнаружит таймаут и сделает restart
    time.sleep(1.0)

    heartbeat.stop()

    # Должен быть новый worker с другим PID
    new_pids = list(pp._workers.keys())
    assert len(new_pids) == 1
    new_pid = new_pids[0]
    assert new_pid != original_pid
    assert pp._workers[new_pid].is_alive()

    pp.shutdown(timeout=3)


def test_multiple_workers_restart():
    """Два worker'а падают — оба перезапускаются."""
    heartbeat = HeartbeatMonitor(timeout=0.2, check_interval=0.1)
    pp = ProcessPool(num_processes=2, heartbeat_monitor=heartbeat)
    pp.start()

    original_pids = sorted(pp._workers.keys())
    assert len(original_pids) == 2

    # Убиваем обоих
    for pid in original_pids:
        os.kill(pid, signal.SIGKILL)
    time.sleep(0.2)

    # Restart первого
    pp._restart_worker(original_pids[0])
    time.sleep(0.1)

    # Restart второго
    pp._restart_worker(original_pids[1])
    time.sleep(0.1)

    # Должно быть 2 живых worker'а с новыми PID
    new_pids = list(pp._workers.keys())
    assert len(new_pids) == 2
    for pid in new_pids:
        assert pid not in original_pids
        assert pp._workers[pid].is_alive()

    pp.shutdown(timeout=3)


def test_restart_disabled_without_heartbeat():
    """Без heartbeat_monitor restart не делается."""
    pp = ProcessPool(num_processes=1)
    pp.start()

    original_pid = list(pp._workers.keys())[0]
    assert pp._restart_enabled is False

    # _restart_worker ничего не делает без heartbeat
    pp._restart_worker(original_pid)

    # Worker остался тот же (мёртвый)
    assert original_pid in pp._workers

    pp.shutdown(timeout=3)


def test_worker_replacement_metrics():
    """После restart метрики корректно обновляются."""
    heartbeat = HeartbeatMonitor(timeout=2.0, check_interval=0.1)
    pp = ProcessPool(num_processes=1, heartbeat_monitor=heartbeat)
    pp.start()

    killed_before = processpool_killed_total._value.get()

    original_pid = list(pp._workers.keys())[0]
    os.kill(original_pid, signal.SIGKILL)
    time.sleep(0.2)

    pp._restart_worker(original_pid)

    # Killed counter вырос на 1
    assert processpool_killed_total._value.get() == killed_before + 1

    pp.shutdown(timeout=3)
