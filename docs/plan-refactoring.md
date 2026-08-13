# План рефакторинга Mia

## Цель

Mia должна автоматически запускать process pool с балансировкой нагрузки на основе CPU, как HA Proxy.

## Требования

1. **Auto-Scaling Process Pool** — при запуске N воркеров (по числу ядер), каждый привязан к ядру (CPU affinity, Linux)
2. **CPU-based Load Balancing** — weighted scoring: `score = 0.7 × cpu_load + 0.2 × active_tasks + 0.1 × stale_penalty`
3. **Fault Tolerance** — heartbeat каждые 5 сек, auto-restart, graceful shutdown
4. **Убрать дублирование** — убрать uvicorn.run и простые решения, Mia сама управляет потоками
5. **Database и Auth сущности** — Facade + Provider + NullObject

---

## Фаза 1: Интерфейсы и типы

### Шаг 1.1: Добавить интерфейсы в `mia/interfaces.py`

- `ICpuMetricsCollector` — сбор метрик CPU
- `ILoadBalancer` — балансировщик нагрузки
- `IWorkerManager` — управление lifecycle воркеров
- `IDatabaseProvider` — провайдер Database (реализуется belle_db)
- `IAuthProvider` — провайдер Auth (реализуется belle_auth)

### Шаг 1.2: Добавить метрики в `mia/metrics.py`

- `loadbalancer_score_histogram`
- `loadbalancer_selections_total`
- `cpu_load_gauge`
- `stale_penalty_gauge`

---

## Фаза 2: CpuMetricsCollector

### Шаг 2.1: Создать `mia/cpu_metrics.py`

- Чтение `/proc/<pid>/stat` (Linux)
- Вычисление delta jiffies
- Нормализация на ядра
- Фоновый сбор каждую секунду
- Thread-safe через RLock

---

## Фаза 3: Load Balancer

### Шаг 3.1: Создать `mia/load_balancer.py`

- `WorkerState` — состояние воркера (cpu_load, active_tasks, stale_penalty, last_heartbeat)
- `LoadBalancer` — weighted scoring
- Выбор наименее загруженного воркера

---

## Фаза 4: WorkerManager

### Шаг 4.1: Создать `mia/worker_manager.py`

- `WorkerProcess` — обёртка над воркером
- `WorkerManager` — spawn/restart/shutdown воркеров
- Привязка к ядрам через CPU affinity

---

## Фаза 5: Обновление Application

### Шаг 5.1: Обновить `mia/application.py`

- DI для `ICpuMetricsCollector`, `ILoadBalancer`, `IWorkerManager`
- Автозапуск process pool в `startup()`
- Shutdown hook через `ShutdownManager`

### Шаг 5.2: Обновить `mia/process_pool.py`

- Убрать `_worker_entry` (перенести в `worker_process.py`)
- `submit()` вызывает `load_balancer.select_worker()`

---

## Фаза 6: Database и Auth сущности

### Шаг 6.1: Создать `mia/entities/base.py`

- `EntityFacade` — базовый класс с thread-safe сменой провайдера

### Шаг 6.2: Создать `mia/entities/database.py`

- `IDatabase` — интерфейс
- `Database` — фасад
- `NullDatabase` — заглушка

### Шаг 6.3: Создать `mia/entities/auth.py`

- `IAuthProvider` — интерфейс
- `Auth` — фасад
- `NullAuth` — заглушка

### Шаг 6.4: Обновить `belle_db/__init__.py`

- `on_load()`: `state.services.resolve(IDatabase).set_provider(self)`
- `on_unload()`: `state.services.resolve(IDatabase).reset_provider()`

### Шаг 6.5: Обновить `belle_auth/__init__.py`

- Аналогично belle_db

---

## Фаза 7: Упрощение main.py

### Шаг 7.1: Обновить `main.py`

- Убрать `uvicorn.run()`
- Application сама управляет воркерами
- Only graceful shutdown

---

## Фаза 8: Тесты

### Шаг 8.1: Тесты для CpuMetricsCollector

- Тест чтения `/proc`
- Тест вычисления delta

### Шаг 8.2: Тесты для LoadBalancer

- Тест weighted scoring
- Тест выбора воркера

### Шаг 8.3: Тесты для WorkerManager

- Тест spawn/restart/shutdown

### Шаг 8.4: Интеграционные тесты

- Тест автозапуска process pool
- Тест балансировки нагрузки

---

## Порядок реализации

```
1. interfaces.py + metrics.py (база)
2. cpu_metrics.py (сбор метрик)
3. load_balancer.py (балансировка)
4. worker_manager.py (управление воркерами)
5. application.py (интеграция)
6. process_pool.py (упрощение)
7. entities/ (Database, Auth)
8. main.py (упрощение)
9. Тесты
```

---

## Риски и митигация

| Риск | Митигация |
|------|-----------|
| `/proc` недоступен | Fallback на psutil |
| CPU affinity не работает | Graceful degradation |
| Воркер падает при старте | Retry с exponential backoff |
| Memory leak в воркерах | HeartbeatMonitor + auto-restart |

---

## Новые файлы

```
mia/
├── cpu_metrics.py         # CpuMetricsCollector
├── load_balancer.py       # LoadBalancer + WorkerState
├── worker_manager.py      # WorkerManager
├── worker_process.py      # WorkerProcess
└── entities/
    ├── __init__.py
    ├── base.py            # EntityFacade
    ├── database.py        # Database + NullDatabase
    └── auth.py            # Auth + NullAuth
```

## Удаляемые файлы/код

- `_worker_entry` в `process_pool.py` (перенести в `worker_process.py`)
- `uvicorn.run()` в `main.py`
- `create_process_pool()` в `application.py` (заменить на `WorkerManager.start()`)
