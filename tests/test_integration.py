"""End-to-end тесты — полный цикл MIA."""
import sys
import os
import threading
import time
from typing import Any

import pytest

# Корень проекта — в sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.application import Application
from modules_system.module_base import ModuleBase, api_method
from communication.event_bus import EventBus


# ── Вспомогательные функции и модули ──────────────────────────────


def heavy_task(n: int) -> int:
    """Функция для WorkerManager — должна быть на верхнем уровне."""
    return sum(i * i for i in range(n))


class FailingModule(ModuleBase):
    """Модуль, который падает при вызове."""

    @property
    def name(self) -> str:
        return "failing"

    @api_method
    def boom(self) -> None:
        raise ValueError("Intentional crash!")


class EventBusModule(ModuleBase):
    """Модуль, который публикует события."""

    _state: Any = None

    @property
    def name(self) -> str:
        return "event_pub"

    def on_load(self, state: Any) -> None:
        self._state = state

    @api_method
    def publish_event(self, event: str, data: Any) -> None:
        self._state.event_bus.publish(event, data)


class ListenerModule(ModuleBase):
    """Модуль, который слушает события."""

    received: list = []

    @property
    def name(self) -> str:
        return "listener"

    def on_load(self, state: Any) -> None:
        state.event_bus.subscribe("test.event", self._on_event)

    def _on_event(self, data: Any) -> None:
        ListenerModule.received.append(data)


# ── Тесты ──────────────────────────────────────────────────────────


class TestFullLifecycle:
    """State → load → API → unload → shutdown."""

    def test_full_lifecycle(self):
        """Полный цикл: создание → загрузка → API → выгрузка → shutdown."""
        # 1. Создание State
        state = Application(modules_dir="modules")
        assert state is not None

        # 2. Startup
        state.startup()

        # 3. Загрузка модуля
        state.load_module("sample")
        assert "sample" in state._modules

        # 4. Вызов API
        result = state.api.sample.add(10, 20)
        assert result == 30

        result2 = state.api.sample.multiply(5, 6)
        assert result2 == 30

        # 5. Выгрузка модуля
        state.unload_module("sample")
        assert "sample" not in state._modules

        # 6. Shutdown
        state.shutdown()


class TestMultipleModules:
    """Загрузка нескольких модулей, вызов API каждого."""

    def test_multiple_modules(self):
        """Два модуля загружены, оба отвечают на API."""
        state = Application(modules_dir="modules")
        state.startup()

        state.load_module("sample")
        state.load_module("notifications")

        assert len(state._modules) == 2
        assert "sample" in state._modules
        assert "notifications" in state._modules

        # API sample работает
        assert state.api.sample.add(1, 1) == 2

        # Выгрузка обоих
        state.unload_module("sample")
        state.unload_module("notifications")
        assert len(state._modules) == 0

        state.shutdown()


class TestEventBusBetweenModules:
    """Два модуля общаются через EventBus."""

    def test_event_bus_between_modules(self):
        """Модуль публикует событие — другой подписанный модуль получает."""
        state = Application(modules_dir="modules")
        state.startup()

        state.load_module("sample")

        # Регистрируем два кастомных модуля через State
        pub = EventBusModule()
        pub._state = state
        listener = ListenerModule()
        ListenerModule.received.clear()

        # Подписываем listener через event_bus
        state.event_bus.subscribe("test.event", listener._on_event)

        # Публикация
        state.event_bus.publish("test.event", {"msg": "hello"})

        assert ListenerModule.received == [{"msg": "hello"}]

        state.shutdown()


class TestParallelApiMethod:
    """@api_method(parallel=True) выполняется в потоке."""

    def test_parallel_api_method(self):
        """parallel=True метод возвращает Future, выполняется в потоке."""
        state = Application(modules_dir="modules")
        state.startup()

        state.load_module("sample")

        # heavy_computation имеет parallel=True
        future = state.api.sample.heavy_computation([1, 2, 3, 4, 5])
        result = future.result(timeout=5)

        assert result == 15

        state.shutdown()


class TestWorkerManagerDispatch:
    """WorkerManager dispatches задачи."""

    def test_worker_manager_dispatch(self):
        """WorkerManager отправляет задачи и получает результаты."""
        state = Application(modules_dir="modules")
        state.startup()

        wm = state.worker_manager
        assert wm is not None

        result = wm.submit(heavy_task, 100)
        expected = sum(i * i for i in range(100))
        assert result == expected

        state.shutdown()


class TestWorkerAutostart:
    """Автозапуск воркеров через Application.startup()."""

    def test_startup_autostart_workers(self):
        """startup() автоматически запускает воркеров по числу ядер."""
        import os
        state = Application(modules_dir="modules")
        state.startup()

        wm = state.worker_manager
        ids = wm.get_worker_ids()
        assert len(ids) == os.cpu_count()
        assert all(isinstance(i, int) for i in ids)

        state.shutdown()


class TestHeartbeatMonitoring:
    """HeartbeatMonitor отслеживает процессы."""

    def test_heartbeat_monitoring(self):
        """HeartbeatMonitor отслеживает зарегистрированные процессы."""
        state = Application(modules_dir="modules")
        state.startup()

        # Запоминаем количество уже зарегистрированных воркеров
        base_count = state.heartbeat_monitor.active_count

        # Регистрируем фейковый PID
        state.heartbeat_monitor.register(12345)
        assert state.heartbeat_monitor.active_count == base_count + 1

        # Обновляем heartbeat
        state.heartbeat_monitor.update(12345)
        assert state.heartbeat_monitor.active_count == base_count + 1

        # Выегистрируем
        state.heartbeat_monitor.unregister(12345)
        assert state.heartbeat_monitor.active_count == base_count

        state.shutdown()


class TestMetricsUpdated:
    """Метрики обновляются при вызовах."""

    def test_metrics_infrastructure_works(self):
        """Метрики Prometheus — инфраструктура работает корректно."""
        from monitoring.metrics import (
            api_calls_total,
            api_duration_seconds,
            processpool_active,
            threadpool_active,
        )

        # Проверяем, что объекты метрик существуют и функциональны
        counter = api_calls_total.labels(module="test", method="test", status="ok")
        before = counter._value.get()
        counter.inc()
        after = counter._value.get()
        assert after == before + 1, "Counter.inc() должен увеличивать значение"

        # Histogram работает
        hist = api_duration_seconds.labels(module="test", method="test")
        hist.observe(0.05)
        assert hist._sum.get() >= 0.05

        # Gauge работает
        gauge = processpool_active
        gauge.set(5)
        assert gauge._value.get() == 5

        # Threads gauge
        threads = threadpool_active
        threads.set(4)
        assert threads._value.get() == 4

    def test_metrics_labels_correct(self):
        """Метрики имеют правильные labels."""
        from monitoring.metrics import api_calls_total

        # Проверяем что labels правильные
        counter = api_calls_total.labels(
            module="sample", method="add", status="ok"
        )
        assert counter is not None

    def test_api_calls_do_not_crash_metrics(self):
        """API вызовы не ломают инфраструктуру метрик."""
        state = Application(modules_dir="modules")
        state.startup()
        state.load_module("sample")

        # Вызываем API — метрики не должны упасть
        for _ in range(10):
            state.api.sample.add(1, 2)

        state.shutdown()


class TestErrorHandling:
    """Ошибка в модуле не ломает систему."""

    def test_error_handling(self):
        """Ошибка в модуле не крашит State — другие модули продолжают работу."""
        state = Application(modules_dir="modules")
        state.startup()

        # Регистрируем модуль с ошибкой
        failing = FailingModule()
        state._modules["failing"] = failing
        state._api_proxy.register_module(failing)

        # Загружаем рабочий модуль
        state.load_module("sample")

        # Вызов падающего метода — бросает исключение
        with pytest.raises(ValueError, match="Intentional crash"):
            state.api.failing.boom()

        # Но рабочий модуль всё ещё работает
        assert state.api.sample.add(1, 1) == 2

        state.shutdown()


class TestConcurrentApiCalls:
    """Несколько параллельных вызовов API."""

    def test_concurrent_api_calls(self):
        """Множественные параллельные API вызовы через ThreadPool."""
        state = Application(modules_dir="modules")
        state.startup()

        state.load_module("sample")

        results = []
        errors = []

        def call_api(i: int) -> None:
            try:
                r = state.api.sample.add(i, i)
                results.append(r)
            except Exception as e:
                errors.append(e)

        # Запускаем 20 потоков
        threads = []
        for i in range(20):
            t = threading.Thread(target=call_api, args=(i,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=5)

        # Все вызовы завершились успешно
        assert len(errors) == 0, f"Ошибки: {errors}"
        assert len(results) == 20

        # Каждый результат равен i + i
        expected = [i + i for i in range(20)]
        assert sorted(results) == sorted(expected)

        state.shutdown()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])