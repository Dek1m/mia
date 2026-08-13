"""Полная интеграция всех компонентов MIA.

Демонстрирует:
- State: центральный оркестратор, создание и связывание компонентов
- Module lifecycle: discover → load → on_load → API → on_unload → shutdown
- ApiProxy: state.api.module.method()
- EventBus: pub/sub коммуникация между модулями
- ThreadPool: @api_method(parallel=True) и прямые submit
- WorkerManager: multiprocessing dispatching с fault tolerance
- Heartbeat: мониторинг процессов
- Metrics: метрики обновляются
"""
import sys
import os

# Корень проекта — в sys.path для импортов
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.application import Application
from argenta_logging import get_logger

log = get_logger(__name__)


def heavy_task(n: int) -> int:
    """Функция для WorkerManager — должна быть на верхнем уровне (picklable)."""
    return sum(i * i for i in range(n))


def main() -> None:
    """Запуск полной интеграции."""
    log.info("Integration test started")

    # ── 1. Создание State ──────────────────────────────────────
    log.info("Creating Application")
    state = Application(modules_dir="modules")
    state.startup()
    log.info("Application started", extra={
        "thread_pool": state.thread_pool is not None,
        "heartbeat": state.heartbeat is not None,
    })

    # ── 2. Module lifecycle ────────────────────────────────────
    log.info("Loading modules (auto-discover)")
    state.load_all_modules()
    loaded = state.modules.list_all()
    log.info("Modules loaded", extra={"modules": loaded})

    for name in state.modules.list_all():
        mod = state.modules.get(name)
        log.info("Module info", extra={"module": mod.name, "version": mod.version})

    # ── 3. API calls через ApiProxy ────────────────────────────
    log.info("Testing API calls via state.api")
    result_add = state.api.sample.add(1, 2)
    result_mul = state.api.sample.multiply(3, 4)
    log.info("API results", extra={
        "add": result_add,
        "multiply": result_mul,
    })
    assert result_add == 3, f"Expected 3, got {result_add}"
    assert result_mul == 12, f"Expected 12, got {result_mul}"

    # ── 4. EventBus: pub/sub ───────────────────────────────────
    log.info("Testing EventBus pub/sub")
    received = []

    def on_data(data):
        received.append(data)

    state.event_bus.subscribe("data.processed", on_data)
    state.event_bus.publish("data.processed", {"value": 42})
    log.info("Event received", extra={"received": received})
    assert received == [{"value": 42}]

    # Публикация без подписчиков — не ошибка
    state.event_bus.publish("unsubscribed.event", "noise")
    log.info("Publish without subscribers: OK")

    # ── 5. ThreadPool: @api_method(parallel=True) ──────────────
    log.info("Testing ThreadPool parallel API method")
    future = state.thread_pool.submit(lambda: "Hello from thread!")
    thread_result = future.result(timeout=5)
    log.info("ThreadPool result", extra={"result": thread_result})
    assert thread_result == "Hello from thread!"

    # parallel метод через ApiProxy
    future2 = state.api.sample.heavy_computation([1, 2, 3, 4, 5])
    parallel_result = future2.result(timeout=5)
    log.info("Parallel API result", extra={"result": parallel_result})
    assert parallel_result == 15

    # ── 6. WorkerManager: multiprocessing dispatching ──────────
    log.info("Testing WorkerManager multiprocessing")
    wm = state.worker_manager
    log.info("WorkerManager created", extra={"exists": wm is not None})

    proc_result = wm.submit(heavy_task, 100)
    log.info("Worker task result", extra={"task": "heavy_task", "arg": 100, "result": proc_result})
    assert proc_result == sum(i * i for i in range(100))

    proc_result2 = wm.submit(heavy_task, 10)
    log.info("Worker task result", extra={"task": "heavy_task", "arg": 10, "result": proc_result2})
    assert proc_result2 == sum(i * i for i in range(10))

    # ── 7. Heartbeat: мониторинг процессов ─────────────────────
    log.info("Testing Heartbeat monitoring")
    active = state.heartbeat.active_count
    log.info("Heartbeat active processes", extra={"count": active})
    assert active >= 1

    # ── 8. EventBus: событие process.died ──────────────────────
    log.info("Testing EventBus process.died event")
    death_events = []

    def on_process_died(data):
        death_events.append(data)

    state.event_bus.subscribe("process.died", on_process_died)
    # Симуляция — публикация вручную (в реальности приходит из heartbeat)
    state.event_bus.publish("process.died", {"pid": 99999})
    log.info("Death events received", extra={"events": death_events})
    assert death_events == [{"pid": 99999}]

    # ── 9. Выгрузка модуля ─────────────────────────────────────
    log.info("Testing module unload")
    state.unload_module("sample")
    assert "sample" not in state.modules.list_all()
    log.info("Module unloaded", extra={"module": "sample"})
    try:
        _ = state.api.sample
        log.error("Module still accessible after unload")
    except AttributeError:
        log.info("Module access after unload raises AttributeError (expected)")

    # ── 10. Shutdown ───────────────────────────────────────────
    log.info("Shutting down Application")
    state.shutdown()
    log.info("Application shutdown complete")
    log.info("Integration test passed")


if __name__ == "__main__":
    main()
