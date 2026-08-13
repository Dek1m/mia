# План реализации Mio — State Manager + Module System + Multiprocessing Dispatching

> **Версия:** 0.0.0  
> **Дата:** 2026-08-06  
> **Проект:** mia (папка: mia)  
> **Пакет:** mia  

---

## Обзор

**Mio** — Python-библиотека, предоставляющая:
1. **State Manager** — центральный оркестратор, управляет процессами, потоками, модулями
2. **Module System** — модули в папке `/modules`, подключаются через `__init__.py`, регистрируют API
3. **Multiprocessing Dispatching** — каждое API-вызове через state автоматически проходит через process pool с CPU affinity
4. **Cross-platform CPU Affinity** — Linux=native, Windows=psutil, graceful degradation

### Ключевые архитектурные решения (согласованы с @architect):
- Pull-модель для распределения (процессы сами берут задачи)
- Shared memory для данных
- Pickle для сериализации
- Graceful degradation для affinity
- Иерархическое управление: StateManager → ProcessManager → ThreadPoolManager

---

## Структура проекта

```
mia/
├── pyproject.toml
├── README.md
├── .gitignore
├── PLAN.md              (gitignored)
├── modules/
│   └── .gitkeep
├── mia/
│   ├── __init__.py
│   ├── state.py          # Точка входа: state = State()
│   ├── module_base.py    # Базовый класс модуля (ModuleBase)
│   ├── module_manager.py # Автосканирование /modules, загрузка
│   ├── api_proxy.py      # Динамическая прокси-структура state.api.*
│   ├── api_registry.py   # Реестр зарегистрированных API методов
│   ├── event_bus.py      # События между модулями
│   ├── thread_pool.py    # ThreadPoolManager (внутри процесса)
│   ├── process_pool.py   # ProcessPool с affinity, heartbeat, fault tolerance
│   ├── cpu_affinity.py   # CpuAffinityProvider (Linux/Windows)
│   ├── heartbeat_monitor.py # Мониторинг живых процессов
│   ├── shared_memory.py  # Управление shared memory
│   ├── serializer.py     # Pickle сериализация
│   ├── logger.py         # Обёртка над argenta-logging (setup_logging + get_logger)
│   ├── metrics.py        # Prometheus метрики
│   └── errors.py         # Кастомные исключения
└── tests/
    ├── __init__.py
    └── test_state.py
```

---

## Стандарты Argenta Team

### Логирование

Используем нашу библиотеку **[`argenta-logging`](https://github.com/Dek1m/argenta-logging)** (`pip install argenta-logging`).

**Референс:** `argenta_logging` — унифицированное логирование для сервисов Argenta Team.

```python
from argenta_logging import setup_logging, get_logger, measure_duration, request_id_var

# Инициализация при старте
setup_logging(service="mia", level="INFO", fmt="posix")

logger = get_logger(__name__)
logger.info("API call", extra={"module": "pdf_worker", "method": "read"})

# Замер длительности
with measure_duration(logger, "process_chunk"):
    process(chunk)

# Request tracing через contextvars
request_id_var.set("req-123")
```

- **4 уровня:** DEBUG, INFO, WARN, ERROR (Python WARNING → WARN, CRITICAL → ERROR)
- **Форматы:** `"posix"` — `[ISO8601] [LEVEL] [service] msg {json}`, `"json"` — полный JSON
- **Вывод:** stdout (по умолчанию)
- **Request tracing:** `request_id_var` через `contextvars` — корреляция вызовов через все слои
- **Замер длительности:** `measure_duration(logger, message)` — context manager, логирует duration_ms
- **Логировать:** каждый вызов API (entry/exit с duration_ms), spawn/kill процессов, heartbeat, ошибки

### Код
- Python 3.10+
- type hints для всех публичных функций
- docstrings для всех публичных классов/методов
- pytest для тестов

### Метрики (Prometheus)

Метрики экспортятся через `prometheus_client` на порту `:9090/metrics`.

**Обязательные метрики:**

| Метрика | Тип | Описание |
|---------|-----|----------|
| `state_api_calls_total` | Counter | Счётчик вызовов API (labels: module, method, status) |
| `state_api_duration_seconds` | Histogram | Время выполнения API-вызова (labels: module, method) |
| `state_processes_active` | Gauge | Количество активных процессов |
| `state_processes_spawned_total` | Counter | Счётчик запущенных процессов |
| `state_processes_killed_total` | Counter | Счётчик завершённых процессов |
| `state_threads_active` | Gauge | Количество активных потоков |
| `state_tasks_queue_size` | Gauge | Размер очереди задач |
| `state_tasks_completed_total` | Counter | Счётчик завершённых задач (labels: status) |
| `state_tasks_failed_total` | Counter | Счётчик упавших задач (labels: error_type) |
| `state_heartbeat_missed_total` | Counter | Счётчик пропущенных heartbeat |
| `state_memory_bytes` | Gauge | Использование shared memory (labels: segment) |
| `state_module_loads_total` | Counter | Счётчик загрузок модулей (labels: module, status) |

**Экспорт:**
```python
from mia.metrics import MetricsServer
server = MetricsServer(port=9090)  # запуск в отдельном потоке
```

**Интеграция:** Каждый компонент (State, ProcessPool, ThreadPool, ModuleManager) инкрементирует свои метрики автоматически.

---

## Фаза 0: Scaffold + базовое ядро (2–3 дня)

**Цель:** Minimum viable core — `State()` создаётся, один простой модуль загружается и регистрирует API.

| Шаг | Что делаем | Ответственный | Файлы |
|-----|-----------|---------------|-------|
| 0.1 | Инициализация проекта: `pyproject.toml`, структура каталогов, `.gitignore` | **Рэй** | `pyproject.toml`, `.gitignore`, `mia/__init__.py` |
| 0.2 | Базовый `ModuleBase` с интерфейсом: `on_load()`, `on_unload()`, декоратор `@api_method` | **Сона** | `mia/module_base.py` |
| 0.3 | `ModuleManager` — автосканирование `/modules`, импорт модулей через `__init__.py` | **Сона** | `mia/module_manager.py` |
| 0.4 | `State` — точка входа: `state = State()`, реестр модулей, вызов `on_load` при загрузке | **Сона** | `mia/state.py` |
| 0.5 | Простой тестовый модуль `sample_module` с 2-3 `@api_method` | **Сона** | `modules/sample/__init__.py` |
| 0.6 | Unit-тесты: ModuleBase, ModuleManager, State | **Катерина** | `tests/test_module_base.py`, `tests/test_module_manager.py`, `tests/test_state.py` |
| 0.7 | Логирование: `setup_logging(service="mia")` через `argenta-logging` | **Мая** | `mia/logger.py` |
| 0.8 | Документация: README, docstrings | **Тиамат** | `README.md` |
| 0.9 | Метрики: `mia/metrics.py` — базовый MetricsServer + все метрики из таблицы | **Мая** | `mia/metrics.py` |

**Метрика готовности:** `state = State(); state.load_module("sample")` работает, `state.api.sample.math.add(1, 2)` → `3`.

---

## Фаза 1: API Proxy — динамическая структура вызова (1–2 дня)

**Цель:** Пользователь вызывает `state.api.{module}.{namespace}.{method}()` — прокси перенаправляет вызов в правильный модуль.

| Шаг | Что делаем | Ответственный | Файлы |
|-----|-----------|---------------|-------|
| 1.1 | `ApiProxy` — динамический `__getattr__` для построения цепочки | **Сона** | `mia/api_proxy.py` |
| 1.2 | Реестр API: при регистрации `@api_method` модуль добавляет путь в реестр | **Сона** | `mia/api_registry.py` |
| 1.3 | Интеграция ApiProxy с State: `state.api` возвращает прокси | **Сона** | обновления в `mia/state.py` |
| 1.4 | Тесты: вызовы через прокси, несуществующие модули/методы, ошибки | **Катерина** | `tests/test_api_proxy.py` |
| 1.5 | Логирование: логировать каждый вызов API (модуль, метод, аргументы) | **Мая** | обновления в `mia/logger.py` |

**Метрика готовности:** `state.api.sample.math.add(1, 2)` работает через прокси, в логах видно `[INFO] [api] sample.math.add called`.

---

## Фаза 2: Event Bus — события между модулями (1 день)

**Цель:** Модули могут подписываться и публиковать события.

| Шаг | Что делаем | Ответственный | Файлы |
|-----|-----------|---------------|-------|
| 2.1 | `EventBus` — `subscribe(event, handler)`, `publish(event, data)` | **Сона** | `mia/event_bus.py` |
| 2.2 | Интеграция EventBus с State: `state.event_bus` | **Сона** | обновления в `mia/state.py` |
| 2.3 | Пример: модуль `notifications` подписывается на событие `data.processed` | **Сона** | `modules/notifications/__init__.py` |
| 2.4 | Тесты: подписка/публикация, множественные подписчики | **Катерина** | `tests/test_event_bus.py` |

**Метрика готовности:** Два модуля общаются через EventBus, в логах видны события.

---

## Фаза 3: ThreadPoolManager — многопоточность внутри процесса (1–2 дня)

**Цель:** Каждый модуль может выполнять задачи в пуле потоков.

| Шаг | Что делаем | Ответственный | Файлы |
|-----|-----------|---------------|-------|
| 3.1 | `ThreadPoolManager` — создание/управление пулом потоков, heartbeat | **Сона** | `mia/thread_pool.py` |
| 3.2 | Декоратор `@api_method(parallel=True)` — запуск в потоке | **Сона** | обновления в `mia/module_base.py` |
| 3.3 | Тесты: параллельные вызовы, таймауты, heartbeat | **Катерина** | `tests/test_thread_pool.py` |
| 3.4 | Логирование: spawn/kill потоков, heartbeat | **Мая** | обновления в `mia/logger.py` |

**Метрика готовности:** `@api_method(parallel=True)` выполняет функцию в отдельном потоке.

---

## Фаза 4: CpuAffinity + ProcessPool — multiprocessing с affinity (3–4 дня)

**Цель:** Каждый процесс привязан к CPU-ядрам. Pull-модель для распределения.

| Шаг | Что делаем | Ответственный | Файлы |
|-----|-----------|---------------|-------|
| 4.1 | `CpuAffinityProvider` — абстракция: `get_cpu_count()`, `set_affinity(pid, cores)` | **Сона** | `mia/cpu_affinity.py` |
| 4.2 | Linux=native (`os.sched_setaffinity`), Windows=psutil | **Сона** | `mia/cpu_affinity.py` |
| 4.3 | Graceful degradation: если affinity недоступен — продолжить без привязки | **Сона** | обновления в `mia/cpu_affinity.py` |
| 4.4 | `ProcessPool` — пул процессов с affinity, heartbeat, fault tolerance | **Сона** | `mia/process_pool.py` |
| 4.5 | Pull-модель: процессы сами берут задачи из очереди | **Сона** | обновления в `mia/process_pool.py` |
| 4.6 | Shared memory: `multiprocessing.shared_memory` для передачи данных | **Сона** | `mia/shared_memory.py` |
| 4.7 | Pickle сериализация | **Сона** | `mia/serializer.py` |
| 4.8 | Тесты: spawn/kill процессов, affinity, heartbeat | **Катерина** | `tests/test_process_pool.py`, `tests/test_cpu_affinity.py` |
| 4.9 | Логирование: spawn/kill процессов, heartbeat, ошибки | **Мая** | обновления в `mia/logger.py` |

**Метрика готовности:** 4 процесса запускаются, каждый привязан к своему ядру (видно в `taskset` на Linux).

---

## Фаза 5: HeartbeatMonitor + Fault Tolerance (2 дня)

**Цель:** Мониторинг живых процессов, автоматический перезапуск мёртвых.

| Шаг | Что делаем | Ответственный | Файлы |
|-----|-----------|---------------|-------|
| 5.1 | `HeartbeatMonitor` — отслеживание heartbeat, таймауты | **Сона** | `mia/heartbeat_monitor.py` |
| 5.2 | Fault tolerance: перезапуск процесса при пропущенном heartbeat | **Сона** | обновления в `mia/process_pool.py` |
| 5.3 | EventBus-интеграция: события `process.died`, `process.restarted` | **Сона** | обновления в `mia/event_bus.py` |
| 5.4 | Тесты: убить процесс → перезапуск, таймаут heartbeat | **Катерина** | `tests/test_heartbeat.py` |
| 5.5 | Логирование: heartbeat missed, process died, process restarted | **Мая** | обновления в `mia/logger.py` |

**Метрика готовности:** Убиваем процесс вручную → он перезапускается автоматически.

---

## Фаза 6: Интеграция всех компонентов + полные тесты (2–3 дня)

**Цель:** Полноценная рабочая система. Все компоненты работают вместе.

| Шаг | Что делаем | Ответственный | Файлы |
|-----|-----------|---------------|-------|
| 6.1 | Полная интеграция: State → ModuleManager → ApiProxy → Dispatcher → ProcessPool | **Сона** | обновления во всех модулях |
| 6.2 | End-to-end тест: несколько модулей, параллельные вызовы, affinity, heartbeat | **Катерина** | `tests/test_integration.py` |
| 6.3 | Нагрузочный тест: 100+ параллельных вызовов, мониторинг CPU/RAM | **Катерина** | `tests/test_load.py` |
| 6.4 | Примеры использования: `examples/basic.py`, `examples/parallel.py` | **Тиамат** | `examples/` |
| 6.5 | README: полная документация, quickstart, API reference | **Тиамат** | `README.md` |
| 6.6 | Интеграция метрик: все компоненты инкрементируют Prometheus метрики, тест метрик | **Мая** | `tests/test_metrics.py` |

**Метрика готовности:** Полный цикл: `state = State(); state.load_module("math"); result = state.api.math.add(1, 2); assert result == 3`.

---

## Фаза 7: Polish, документация, деплой (1–2 дня)

**Цель:** Готово к использованию.

| Шаг | Что делаем | Ответственный | Файлы |
|-----|-----------|---------------|-------|
| 7.1 | Аудит безопасности: pickle-safe, sandbox модулей | **Лита** | проверка всех модулей |
| 7.2 | CI/CD: GitHub Actions — lint, typecheck, test, build | **Рэй** | `.github/workflows/ci.yml` |
| 7.3 | Type hints + mypy: полная типизация | **Сона** | все `.py` файлы |
| 7.4 | Docstrings для всех публичных классов/функций | **Тиамат** | все `.py` файлы |
| 7.5 | Финальные тесты: edge cases, error handling | **Катерина** | `tests/` |
| 7.6 | Публикация в PyPI (опционально) | **Рэй** | `pyproject.toml` |

---

## Зависимости

```
[project]
dependencies = ["argenta-logging>=0.1.0"]
[project.optional-dependencies]
metrics = ["prometheus-client>=0.20.0"]
dev = ["pytest>=7.0", "prometheus-client>=0.20.0"]
```

---

## Оценка времени

| Фаза | Описание | Оценка |
|------|----------|--------|
| 0 | Scaffold + ядро | 2–3 дня |
| 1 | API Proxy | 1–2 дня |
| 2 | Event Bus | 1 день |
| 3 | ThreadPool | 1–2 дня |
| 4 | CpuAffinity + ProcessPool | 3–4 дня |
| 5 | Heartbeat + Fault Tolerance | 2 дня |
| 6 | Интеграция + тесты | 2–3 дня |
| 7 | Polish + деплой | 1–2 дня |
| **Итого** | | **13–19 дней** |

---

## Порядок коммитов/PR

| PR | Содержание | Автор |
|----|-----------|-------|
| 1 | Scaffold проекта (Фаза 0.1) | **Рэй** |
| 2 | ModuleBase + ModuleManager + State (Фаза 0.2–0.4) | **Сона** |
| 3 | Тесты ядра + логирование (Фаза 0.6–0.7) | **Катерина**, **Мая** |
| 4 | API Proxy (Фаза 1) | **Сона** |
| 5 | Event Bus (Фаза 2) | **Сона** |
| 6 | ThreadPool (Фаза 3) | **Сона** |
| 7 | CpuAffinity + ProcessPool (Фаза 4) | **Сона** |
| 8 | Heartbeat + Fault Tolerance (Фаза 5) | **Сона** |
| 9 | Интеграционные тесты (Фаза 6) | **Катерина** |
| 10 | Polish + CI/CD + docs (Фаза 7) | **Все** |

---

## Риски

| Риск | Вероятность | Влияние | Митигация |
|------|-------------|---------|-----------|
| Pickle небезопасен для сторонних модулей | средняя | высокое | RestrictedUnpickler, sandboxing |
| ProcessPool сложнее чем кажется | средняя | высокое | Постепенное усложнение |
| Windows-совместимость affinity | низкая | среднее | Graceful degradation |
| Deadlock в pull-модели | низкая | высокое | Таймауты + тесты |
| Производительность shared memory | средняя | среднее | Бенчмарки |

---

## Метрики готовности (итоговые)

- [ ] `State()` создаётся без ошибок
- [ ] Модуль загружается через `state.load_module("name")`
- [ ] API вызывается через `state.api.module.namespace.method()`
- [ ] `@api_method(parallel=True)` выполняет в потоке
- [ ] Процессы привязаны к CPU-ядрам (Linux: `taskset -p <pid>`)
- [ ] Heartbeat работает, мёртвые процессы перезапускаются
- [ ] EventBus доставляет события между модулями
- [ ] Все unit-тесты зелёные
- [ ] Интеграционные тесты зелёные
- [ ] Логи в формате `[ISO8601] [LEVEL] [module] message`
- [ ] Type hints, docstrings, README
