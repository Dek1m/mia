"""Unit-тесты для HeartbeatMonitor."""
import logging
import time
from unittest.mock import MagicMock, patch

import pytest

from monitoring.heartbeat_monitor import HeartbeatMonitor


# === Фикстуры ===

@pytest.fixture
def monitor():
    """Создаёт HeartbeatMonitor с коротким таймаутом для быстрых тестов."""
    m = HeartbeatMonitor(timeout=0.3, check_interval=0.1)
    yield m
    m.stop()


@pytest.fixture
def running_monitor():
    """HeartbeatMonitor с запущенным мониторингом — для тестов таймаута."""
    m = HeartbeatMonitor(timeout=0.3, check_interval=0.1)
    m.start()
    yield m
    m.stop()


# === Базовые тесты ===

def test_heartbeat_creation():
    """HeartbeatMonitor() создаётся без ошибок."""
    m = HeartbeatMonitor()
    assert m is not None
    assert isinstance(m, HeartbeatMonitor)
    assert m._timeout == 30.0
    assert m._check_interval == 5.0
    assert m.active_count == 0


def test_heartbeat_creation_custom():
    """HeartbeatMonitor(timeout=5, check_interval=1) с кастомными параметрами."""
    m = HeartbeatMonitor(timeout=5.0, check_interval=1.0)
    assert m._timeout == 5.0
    assert m._check_interval == 1.0


def test_register_process(monitor):
    """register(pid) добавляет процесс в мониторинг."""
    monitor.register(1234)
    assert monitor.active_count == 1
    assert 1234 in monitor._heartbeats


def test_register_with_meta(monitor):
    """register(pid, meta) сохраняет мета-данные."""
    monitor.register(1234, {"worker_id": 7})
    assert monitor.active_count == 1
    assert 1234 in monitor._meta
    assert monitor._meta[1234] == {"worker_id": 7}


def test_register_without_meta(monitor):
    """register(pid) без meta не создаёт запись в _meta."""
    monitor.register(1234)
    assert 1234 not in monitor._meta


def test_register_multiple_processes(monitor):
    """Несколько register(pid) добавляют несколько процессов."""
    monitor.register(100)
    monitor.register(200)
    monitor.register(300)
    assert monitor.active_count == 3


def test_unregister_process(monitor):
    """unregister(pid) убирает процесс из мониторинга."""
    monitor.register(1234)
    assert monitor.active_count == 1
    monitor.unregister(1234)
    assert monitor.active_count == 0
    assert 1234 not in monitor._heartbeats


def test_unregister_clears_meta(monitor):
    """unregister(pid) удаляет и мета-данные."""
    monitor.register(1234, {"worker_id": 3})
    assert 1234 in monitor._meta
    monitor.unregister(1234)
    assert 1234 not in monitor._meta


def test_unregister_nonexistent(monitor):
    """unregister несуществующего pid не падает."""
    monitor.unregister(99999)  # Не должно выбросить исключение
    assert monitor.active_count == 0


def test_unregister_does_not_affect_others(monitor):
    """unregister одного pid не влияет на другие."""
    monitor.register(100)
    monitor.register(200)
    monitor.unregister(100)
    assert monitor.active_count == 1
    assert 200 in monitor._heartbeats


def test_update_heartbeat(monitor):
    """update(pid) обновляет время heartbeat."""
    monitor.register(1234)
    old_time = monitor._heartbeats[1234]
    time.sleep(0.05)
    monitor.update(1234)
    new_time = monitor._heartbeats[1234]
    assert new_time > old_time


def test_update_nonexistent_pid(monitor):
    """update несуществующего pid не падает и не добавляет его."""
    monitor.update(99999)
    assert monitor.active_count == 0


def test_active_count(monitor):
    """active_count возвращает корректное количество процессов."""
    assert monitor.active_count == 0
    monitor.register(1)
    assert monitor.active_count == 1
    monitor.register(2)
    assert monitor.active_count == 2
    monitor.unregister(1)
    assert monitor.active_count == 1


# === Таймаут и handler ===

def test_timeout_handler(running_monitor):
    """При таймауте вызывается handler с pid мёртвого процесса."""
    handler = MagicMock()
    running_monitor.set_timeout_handler(handler)
    running_monitor.register(1234)

    # Ждём пока heartbeat протухнёт (0.3s timeout + 0.1s check interval)
    time.sleep(0.5)

    handler.assert_called_once_with(1234)


def test_timeout_handler_called_for_each_dead(running_monitor):
    """Handler вызывается для каждого мёртвого процесса."""
    handler = MagicMock()
    running_monitor.set_timeout_handler(handler)
    running_monitor.register(100)
    running_monitor.register(200)

    time.sleep(0.5)

    assert handler.call_count == 2
    call_pids = sorted([call.args[0] for call in handler.call_args_list])
    assert call_pids == [100, 200]


def test_no_timeout_within_timeout(running_monitor):
    """Если heartbeat обновляется вовремя — таймаута нет."""
    handler = MagicMock()
    running_monitor.set_timeout_handler(handler)
    running_monitor.register(1234)

    # Обновляем heartbeat каждые 0.1s на протяжении 0.5s
    for _ in range(5):
        time.sleep(0.1)
        running_monitor.update(1234)

    time.sleep(0.1)
    handler.assert_not_called()


def test_timeout_handler_exception_does_not_crash(running_monitor):
    """Если handler бросает исключение — мониторинг продолжает работу."""
    def bad_handler(pid):
        raise RuntimeError("handler crashed")

    running_monitor.set_timeout_handler(bad_handler)
    running_monitor.register(1234)

    # Не должно упасть
    time.sleep(0.5)

    # Мониторинг всё ещё работает
    running_monitor.register(5678)
    assert running_monitor.active_count == 2


# === Meta в логах ===

def test_meta_in_timeout_warning(running_monitor, caplog):
    """Meta (worker_id) попадает в warning при heartbeat timeout."""
    running_monitor.register(42, {"worker_id": 7})

    with caplog.at_level(logging.WARNING, logger="monitoring.heartbeat_monitor"):
        time.sleep(0.5)

    timeout_records = [r for r in caplog.records if "Heartbeat timeout" in r.message]
    assert len(timeout_records) >= 1
    rec = timeout_records[0]
    assert rec.pid == 42
    assert getattr(rec, "worker_id", None) == 7


def test_meta_not_in_warning_without_meta(running_monitor, caplog):
    """Без meta — в warning нет worker_id."""
    running_monitor.register(99)

    with caplog.at_level(logging.WARNING, logger="monitoring.heartbeat_monitor"):
        time.sleep(0.5)

    timeout_records = [r for r in caplog.records if "Heartbeat timeout" in r.message]
    assert len(timeout_records) >= 1
    rec = timeout_records[0]
    assert rec.pid == 99
    assert not hasattr(rec, "worker_id")


# === Update продлевает жизнь ===

def test_update_extends_lifetime(running_monitor):
    """Регулярный update() не даёт процессу быть помеченным как мёртвый."""
    handler = MagicMock()
    running_monitor.set_timeout_handler(handler)
    running_monitor.register(100)

    # Обновляем heartbeat каждые 0.15s (timeout=0.3s) на протяжении 1s
    for _ in range(10):
        time.sleep(0.15)
        running_monitor.update(100)

    time.sleep(0.1)
    handler.assert_not_called()


# === Start/Stop ===

def test_start_stop():
    """start()/stop() запускают и останавливают мониторинг."""
    m = HeartbeatMonitor(timeout=10, check_interval=0.1)
    assert m._running is False
    assert m._thread is None

    m.start()
    assert m._running is True
    assert m._thread is not None
    assert m._thread.is_alive()

    m.stop()
    assert m._running is False
    # Поток должен завершиться
    m._thread.join(timeout=1)
    assert not m._thread.is_alive()


def test_start_idempotent():
    """Повторный start() не создаёт второй поток."""
    m = HeartbeatMonitor(timeout=10, check_interval=0.1)
    m.start()
    first_thread = m._thread

    m.start()  # Должен проигнорировать
    assert m._thread is first_thread

    m.stop()


def test_stop_without_start():
    """stop() без start() не падает."""
    m = HeartbeatMonitor()
    m.stop()  # Не должно выбросить исключение


def test_monitoring_triggers_timeout():
    """Полный цикл: register -> start -> timeout -> handler вызван."""
    handler = MagicMock()
    m = HeartbeatMonitor(timeout=0.2, check_interval=0.1)
    m.set_timeout_handler(handler)
    m.register(42)
    m.start()

    time.sleep(0.6)

    handler.assert_called()
    m.stop()
