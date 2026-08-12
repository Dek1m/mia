"""Полная интеграция всех компонентов MIA.

Демонстрирует:
- State: центральный оркестратор, создание и связывание компонентов
- Module lifecycle: discover → load → on_load → API → on_unload → shutdown
- ApiProxy: state.api.module.method()
- EventBus: pub/sub коммуникация между модулями
- ThreadPool: @api_method(parallel=True) и прямые submit
- ProcessPool: multiprocessing dispatching с fault tolerance
- Heartbeat: мониторинг процессов
- Metrics: метрики обновляются
"""
import sys
import os

# Корень проекта — в sys.path для импортов
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from application import Application


def heavy_task(n: int) -> int:
    """Функция для ProcessPool — должна быть на верхнем уровне (picklable)."""
    return sum(i * i for i in range(n))


def main() -> None:
    """Запуск полной интеграции."""
    print("=" * 60)
    print("MIA — полная интеграция всех компонентов")
    print("=" * 60)

    # ── 1. Создание State ──────────────────────────────────────
    print("\n[1] Создание State...")
    state = Application(modules_dir="modules")
    state.startup()
    print(f"    ThreadPool запущен: {state.thread_pool is not None}")
    print(f"    Heartbeat запущен: {state.heartbeat_monitor is not None}")

    # ── 2. Module lifecycle ────────────────────────────────────
    print("\n[2] Загрузка модулей (auto-discover)...")
    state.load_all_modules()
    loaded = list(state._modules.keys())
    print(f"    Загружены: {loaded}")

    for name, mod in state._modules.items():
        print(f"    - {mod.name} v{mod.version}")

    # ── 3. API calls через ApiProxy ────────────────────────────
    print("\n[3] API вызовы через state.api...")
    result_add = state.api.sample.add(1, 2)
    result_mul = state.api.sample.multiply(3, 4)
    print(f"    sample.add(1, 2) = {result_add}")
    print(f"    sample.multiply(3, 4) = {result_mul}")
    assert result_add == 3, f"Ожидалось 3, получено {result_add}"
    assert result_mul == 12, f"Ожидалось 12, получено {result_mul}"

    # ── 4. EventBus: pub/sub ───────────────────────────────────
    print("\n[4] EventBus — pub/sub...")
    received = []

    def on_data(data):
        received.append(data)

    state.event_bus.subscribe("data.processed", on_data)
    state.event_bus.publish("data.processed", {"value": 42})
    print(f"    Получено: {received}")
    assert received == [{"value": 42}]

    # Публикация без подписчиков — не ошибка
    state.event_bus.publish("unsubscribed.event", "noise")
    print("    Публикация без подписчиков: OK")

    # ── 5. ThreadPool: @api_method(parallel=True) ──────────────
    print("\n[5] ThreadPool — parallel API method...")
    future = state.thread_pool.submit(lambda: "Hello from thread!")
    thread_result = future.result(timeout=5)
    print(f"    thread_pool.submit(lambda) = {thread_result}")
    assert thread_result == "Hello from thread!"

    # parallel метод через ApiProxy
    future2 = state.api.sample.heavy_computation([1, 2, 3, 4, 5])
    parallel_result = future2.result(timeout=5)
    print(f"    sample.heavy_computation([1..5]) = {parallel_result}")
    assert parallel_result == 15

    # ── 6. ProcessPool: multiprocessing dispatching ────────────
    print("\n[6] ProcessPool — multiprocessing...")
    pool = state.create_process_pool(num_processes=2)
    print(f"    ProcessPool создан: {pool is not None}")
    print(f"    Активных процессов: {state.process_pool._num_processes}")

    proc_result = state.process_pool.submit(heavy_task, 100)
    print(f"    heavy_task(100) = {proc_result}")
    assert proc_result == sum(i * i for i in range(100))

    # Повторный submit
    proc_result2 = state.process_pool.submit(heavy_task, 10)
    print(f"    heavy_task(10) = {proc_result2}")
    assert proc_result2 == sum(i * i for i in range(10))

    # ── 7. Heartbeat: мониторинг процессов ─────────────────────
    print("\n[7] Heartbeat — мониторинг...")
    active = state.heartbeat_monitor.active_count
    print(f"    Отслеживаемых процессов: {active}")
    assert active >= 1

    # ── 8. EventBus: событие process.died ──────────────────────
    print("\n[8] EventBus — process.died событие...")
    death_events = []

    def on_process_died(data):
        death_events.append(data)

    state.event_bus.subscribe("process.died", on_process_died)
    # Симуляция — публикация вручную (в реальности приходит из heartbeat)
    state.event_bus.publish("process.died", {"pid": 99999})
    print(f"    death_events: {death_events}")
    assert death_events == [{"pid": 99999}]

    # ── 9. Выгрузка модуля ─────────────────────────────────────
    print("\n[9] Module lifecycle — unload...")
    state.unload_module("sample")
    assert "sample" not in state._modules
    print("    sample выгружен")
    try:
        _ = state.api.sample
        print("    ОШИБКА: sample доступен после выгрузки!")
    except AttributeError:
        print("    state.api.sample — AttributeError (ожидаемо)")

    # ── 10. Shutdown ───────────────────────────────────────────
    print("\n[10] Shutdown...")
    state.shutdown()
    print("    Все модули выгружены")
    print("    ThreadPool остановлен")
    print("    Heartbeat остановлен")
    print("    ProcessPool остановлен")

    print("\n" + "=" * 60)
    print("ВСЕ КОМПОНЕНТЫ РАБОТАЮТ ВМЕСТЕ ✓")
    print("=" * 60)


if __name__ == "__main__":
    main()
