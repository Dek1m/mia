## Задача: Перестройка архитектуры раскидывания задач

**Тип:** архитектура
**Сложность:** высокая
**Стандарт:** не определён (рекомендую обсудить с Афиной создание ADR)

### Контекст

**ADR из памяти:**
- `mia-dispatch-architecture-final`: Application → SmartDispatcher → ThreadPool/WorkerManager (текущая финальная архитектура)
- `auto-scaling-process-pool`: Master Process управляет WorkerManager, LoadBalancer направляет запросы к наименее загруженному воркеру
- `mia_architecture_decisions`: pull-model для распределения задач, hierarchical: StateManager → ProcessManager → ThreadPoolManager. Shared memory + pickle для передачи данных
- `SmartDispatcher`: двухфазная маршрутизация TaskClassifier → AdaptiveRouter, dispatch_async для async-функций

**Текущее состояние (код):**
- `SmartDispatcher`: IO/CPU/GPU/NETWORK/DATABASE → ThreadPool, AGGREGATE → WorkerManager
- `WorkerManager`: multiprocessing.Process, task_queue/result_queue, синхронный submit (блокирует вызывающий поток)
- `ThreadPoolManager`: ThreadPoolExecutor, submit → Future
- `LoadBalancer`: weighted scoring (cpu_load, active_tasks, stale_penalty), но active_tasks не обновляется при отправке задач
- `AdaptiveRouter`: обучение на p95, override типов задач
- `Task`: uuid, status, duration, task_type — уже есть UUID

**Желаемое состояние:**
```
Задача → SmartDispatcher → LoadBalancer → WorkerManager (процесс, привязан к ядру)
  → ThreadPool внутри воркера → SharedMemory → UUID → результат/код завершения
```

**Ограничения:**
- 924 passed / 0 failed тестов — не ломать
- 44/44 E2E — не ломать
- Legacy совместимость (fn._db_type) — сохранить
- CLI/REST, auth/workspace/llm — не трогать

---

### Шаг 1: SharedMemoryManager — хранилище результатов по UUID

- **Файл:** `core/shared_memory.py` (новый)
- **Сложность:** средняя
- **Зависимости:** —
- **Ожидаемый результат:** Класс `SharedMemoryManager` с методами `store(task_id, result)`, `retrieve(task_id)`, `get_exit_code(task_id)`. Использует `multiprocessing.shared_memory.SharedMemory` для данных и `dict[UUID, ExitInfo]` для кодов завершения в main process. Воркеры получают `shm.name` для записи, main process читает по UUID.

**Что сделать:**
1. Создать `core/shared_memory.py`
2. Класс `ExitInfo`: dataclass с `exit_code: int`, `error: str | None`, `completed_at: float`
3. Класс `SharedMemoryManager`:
   - `__init__(self, max_tasks: int = 1000)` — создаёт пул SharedMemory блоков
   - `allocate(task_id: UUID, size: int = 1_000_000) → str` — выделить блок, вернуть имя
   - `store_result(task_id: UUID, result: Any)` — сериализовать result в SharedMemory через pickle
   - `retrieve(task_id: UUID) → Any` — десериализовать из SharedMemory
   - `store_exit(task_id: UUID, exit_code: int, error: str | None = None)` — записать код завершения
   - `get_exit_code(task_id: UUID) → ExitInfo | None` — получить код завершения
   - `cleanup(task_id: UUID)` — освободить SharedMemory блок
   - `shutdown()` — освободить все блоки
4. Thread-safe через `threading.Lock`

**Почему отдельный класс:** Изолирует логику shared memory от WorkerManager и SmartDispatcher. Тестируется отдельно.

---

### Шаг 2: WorkerThreadPool — ThreadPool внутри воркера

- **Файл:** `pools/worker_thread_pool.py` (новый)
- **Сложность:** средняя
- **Зависимости:** шаг 1
- **Ожидаемый результат:** Класс `WorkerThreadPool`, который создаётся внутри каждого worker-процесса. Имеет свой `ThreadPoolExecutor` для параллельного выполнения задач внутри процесса.

**Что сделать:**
1. Создать `pools/worker_thread_pool.py`
2. Класс `WorkerThreadPool`:
   - `__init__(self, max_threads: int = 4)` — конфигурируется из config
   - `start()` — создаёт `ThreadPoolExecutor`
   - `submit(fn, *args, **kwargs) → Future` — отправить задачу в локальный пул
   - `shutdown(wait: bool = True)` — остановить пул
   - `active_count → int` — количество активных задач
3. Конфиг: `pools.worker.thread_pool.max_threads` (по умолчанию 4)

**Почему отдельный класс:** Позволяет тестировать ThreadPool внутри воркера отдельно. WorkerThreadPool — это обёртка над ThreadPoolExecutor с дополнительной логикой (метрики, мониторинг).

---

### Шаг 3: Модификация WorkerManager — ThreadPool внутри воркера

- **Файл:** `pools/worker_manager.py` (изменение)
- **Сложность:** высокая
- **Зависимости:** шаг 2
- **Ожидаемый результат:** Каждый worker-процесс создаёт внутри себя `WorkerThreadPool`. Вместо `fn(*args, **kwargs)` в основном потоке — `worker_thread_pool.submit(fn, *args, **kwargs)`.

**Что сделать:**
1. Изменить `_worker_entry`:
   - Добавить параметр `shm_name: str | None = None` (имя SharedMemory блока)
   - После привязки к ядру — создать `WorkerThreadPool` и `start()`
   - В цикле: `future = worker_thread_pool.submit(fn, *args, **kwargs)` → ждать результат
   - Результат/ошибка → `result_queue.put((request_id, "ok"/"error", result))`
   - При `None` (shutdown) — `worker_thread_pool.shutdown()`, `break`
2. Изменить `_spawn_worker`:
   - Передавать `shm_name` (имя SharedMemory блока для этого воркера)
3. Изменить `submit`:
   - Теперь возвращает `Future` вместо блокирующего результата
   - Использует `LoadBalancer.select_worker()` для выбора воркера
   - Отправляет задачу в `task_queue` выбранного воркера
4. Добавить `get_worker_states() → dict[int, WorkerState]` — для LoadBalancer
5. Обновить `_sync_worker_states()` — читать active_tasks из воркеров через SharedMemory

**Ключевое изменение:** `submit` должен стать async (возвращать Future), чтобы SmartDispatcher мог использовать его в `dispatch_async`.

---

### Шаг 4: Расширение LoadBalancer — активные задачи на воркере

- **Файл:** `pools/load_balancer.py` (изменение)
- **Сложность:** низкая
- **Зависимости:** шаг 3
- **Ожидаемый результат:** LoadBalancer корректно учитывает количество активных задач на каждом воркере при выборе.

**Что сделать:**
1. Добавить метод `select_worker_from(states: dict[int, WorkerState]) → int | None` — выбрать из переданных состояний
2. Убедиться, что `active_tasks` обновляется при каждой отправке задачи
3. Добавить `increment_active(worker_id: int)` и `decrement_active(worker_id: int)` — для atomарного обновления
4. Обновить `_score` — учесть, что `active_tasks` может быть 0 (новый воркер)

---

### Шаг 5: Модификация SmartDispatcher — маршрутизация через WorkerManager

- **Файл:** `pools/smart_dispatcher.py` (изменение)
- **Сложность:** высокая
- **Зависимости:** шаг 3, 4
- **Ожидаемый результат:** `dispatch_async` отправляет задачи в WorkerManager через LoadBalancer, а не в ThreadPool. ThreadPool используется только для legacy-режима (fn._db_type) и write-lock задач.

**Что сделать:**
1. Изменить `dispatch_async`:
   - Для async-функций: `worker_manager.submit(fn, *args, **kwargs)` → Future
   - Для sync-функций: аналогично через `worker_manager.submit`
   - ThreadPool используется ТОЛЬКО для:
     - Legacy-режима (fn._db_type задан и не aggregate)
     - Write-lock задач (fn._db_lock = True)
2. Добавить `_dispatch_to_worker(task, fn, *args, **kwargs)` — новый метод для маршрутизации через WorkerManager
3. Изменить `_dispatch_two_phase`:
   - Все типы (IO, CPU, GPU, NETWORK, DATABASE, AGGREGATE) → WorkerManager
   - ThreadPool — только fallback для write-lock
4. Обновить метрики: `worker_manager_tasks_submitted_total` для всех типов задач
5. Сохранить legacy-логику: `_dispatch_legacy` остаётся без изменений

**Ключевое изменение:** SmartDispatcher больше не различает ThreadPool/WorkerManager по типу задачи. Все задачи идут в WorkerManager. ThreadPool — только для legacy и write-lock.

---

### Шаг 6: Модификация AdaptiveRouter — маршрутизация между воркерами

- **Файл:** `core/adaptive_router.py` (изменение)
- **Сложность:** средняя
- **Зависимости:** шаг 5
- **Ожидаемый результат:** AdaptiveRouter продолжает обучение на p95, но теперь маршрутизирует между воркерами (а не между пулами).

**Что сделать:**
1. Изменить `_OVERLOAD_MAP`:
   - Вместо переключения IO ↔ CPU — переключение между воркерами (выбор другого воркера)
   - Или: выбор воркера с наименьшей нагрузкой для данного типа задач
2. Изменить `override`:
   - Возвращать `worker_id` вместо `TaskType` (или дополнительную информацию для выбора воркера)
   - Или: возвращать `TaskType` как подсказку для LoadBalancer (предпочтительный тип воркера)
3. Добавить `get_worker_recommendation(task: Task) → int | None` — рекомендация по выбору воркера
4. Сохранить обратную совместимость: старый API `override` продолжает работать

**Уточнение:** Поскольку все задачи теперь идут в WorkerManager, AdaptiveRouter должен помогать LoadBalancer выбирать конкретный воркер (а не пул). Это может быть:
- Рекомендация по ядру (core_id) для CPU-bound задач
- Рекомендация по воркеру с наименьшей нагрузкой для IO-bound задач

---

### Шаг 7: Модификация @task декоратора — UUID доступен вызывающему

- **Файл:** `core/task_decorator.py` (изменение)
- **Сложность:** средняя
- **Зависимости:** шаг 5
- **Ожидаемый результат:** UUID задачи доступен вызывающему коду через `task.id`. Декоратор возвращает `TaskFuture` — обёртку над Future с доступом к UUID.

**Что сделать:**
1. Создать класс `TaskFuture`:
   - Наследует `Future` или оборачивает его
   - `task_id: UUID` — UUID задачи
   - `result(timeout=None) → Any` — получить результат (как Future)
   - `get_exit_code() → ExitInfo | None` — получить код завершения из SharedMemory
2. Изменить `_wrap_sync`:
   - Вместо `future.result(timeout)` — вернуть `TaskFuture(future, task_obj.id)`
3. Изменить `_wrap_async`:
   - Аналогично вернуть `TaskFuture`
4. Добавить `task_context()` — контекстный менеджер для получения UUID текущей задачи

**Ключевое изменение:** Вызывающий код может получить UUID задачи и по нему забрать результат/код завершения.

---

### Шаг 8: Обновление core/interfaces.py — новые интерфейсы

- **Файл:** `core/interfaces.py` (изменение)
- **Сложность:** низкая
- **Зависимости:** шаг 1, 2
- **Ожидаемый результат:** Новые интерфейсы для SharedMemory и WorkerThreadPool.

**Что сделать:**
1. Добавить `ISharedMemory`:
   - `allocate(task_id: UUID, size: int) → str`
   - `store_result(task_id: UUID, result: Any)`
   - `retrieve(task_id: UUID) → Any`
   - `store_exit(task_id: UUID, exit_code: int, error: str | None)`
   - `get_exit_code(task_id: UUID) → ExitInfo | None`
   - `cleanup(task_id: UUID)`
   - `shutdown()`
2. Добавить `IWorkerThreadPool`:
   - `start()`
   - `submit(fn, *args, **kwargs) → Future`
   - `shutdown(wait: bool = True)`
   - `active_count → int`
3. Расширить `IWorkerManager`:
   - Добавить `submit` → `Future` (вместо `Any`)
   - Добавить `get_worker_states() → dict[int, WorkerState]`

---

### Шаг 9: Обновление Application — сборка новой архитектуры

- **Файл:** `core/application.py` (изменение)
- **Сложность:** средняя
- **Зависимости:** шаг 1-8
- **Ожидаемый результат:** Application создаёт и собирает все новые компоненты.

**Что сделать:**
1. Добавить `SharedMemoryManager` в DI:
   ```python
   shared_memory = SharedMemoryManager()
   self._services.register(ISharedMemory, shared_memory)
   ```
2. Передать `shared_memory` в `WorkerManagerFactory.create()`
3. Убедиться, что `startup()` вызывает `worker_manager.start()` (уже есть)
4. Убедиться, что `shutdown()` вызывает `shared_memory.shutdown()`
5. Обновить `SmartDispatcher` — передать `worker_manager` вместо `thread_pool` для основной маршрутизации

---

### Шаг 10: Тесты SharedMemoryManager

- **Файл:** `tests/test_shared_memory.py` (новый)
- **Сложность:** средняя
- **Зависимости:** шаг 1
- **Ожидаемый résultat:** Юнит-тесты для SharedMemoryManager.

**Что сделать:**
1. Тест `test_store_and_retrieve` — сохранить и забрать результат
2. Тест `test_store_exit_code` — сохранить и забрать код завершения
3. Тест `test_cleanup` — освободить память
4. Тест `test_concurrent_access` — параллельный доступ
5. Тест `test_max_tasks_limit` — превышение лимита

---

### Шаг 11: Тесты WorkerThreadPool

- **Файл:** `tests/test_worker_thread_pool.py` (новый)
- **Сложность:** средняя
- **Зависимости:** шаг 2
- **Ожидаемый результат:** Юнит-тесты для WorkerThreadPool.

**Что сделать:**
1. Тест `test_submit_and_result` — отправить задачу и получить результат
2. Тест `test_concurrent_tasks` — параллельное выполнение
3. Тест `test_shutdown` — корректная остановка
4. Тест `test_active_count` — подсчёт активных задач

---

### Шаг 12: Обновление тестов SmartDispatcher

- **Файл:** `tests/test_smart_dispatcher.py` (изменение)
- **Сложность:** средняя
- **Зависимости:** шаг 5
- **Ожидаемый результат:** Тесты обновлены для новой архитектуры. Legacy-тесты остаются без изменений.

**Что сделать:**
1. Обновить `FakeWorkerManager` — теперь возвращает Future
2. Добавить тесты для маршрутизации через WorkerManager:
   - `test_io_routes_to_worker_manager` — IO задача идёт в WorkerManager
   - `test_cpu_routes_to_worker_manager` — CPU задача идёт в WorkerManager
   - `test_aggregate_routes_to_worker_manager` — AGGREGATE задача идёт в WorkerManager
3. Добавить тесты для write-lock:
   - `test_write_lock_routes_to_thread_pool` — write-lock задача идёт в ThreadPool
4. Убедиться, что legacy-тесты проходят (fn._db_type)

---

### Шаг 13: Обновление тестов WorkerManager

- **Файл:** `tests/test_worker_manager.py` (изменение)
- **Сложность:** средняя
- **Зависимости:** шаг 3
- **Ожидаемый результат:** Тесты обновлены для новой архитектуры.

**Что сделать:**
1. Обновить тесты `submit` — теперь возвращает Future
2. Добавить тесты для ThreadPool внутри воркера:
   - `test_worker_creates_thread_pool` — воркер создаёт ThreadPool
   - `test_parallel_tasks_in_worker` — параллельное выполнение в воркере
3. Добавить тесты для SharedMemory:
   - `test_result_stored_in_shared_memory` — результат в SharedMemory

---

### Шаг 14: Обновление тестов LoadBalancer

- **Файл:** `tests/test_load_balancer.py` (изменение)
- **Сложность:** низкая
- **Зависимости:** шаг 4
- **Ожидаемый результат:** Тесты обновлены для нового API.

**Что сделать:**
1. Добавить тесты для `select_worker_from`
2. Добавить тесты для `increment_active` / `decrement_active`
3. Убедиться, что существующие тесты проходят

---

### Шаг 15: Интеграционные тесты — E2E

- **Файл:** `tests/e2e/` (проверка)
- **Сложность:** высокая
- **Зависимости:** шаг 1-14
- **Ожидаемый результат:** Все 44 E2E теста проходят.

**Что сделать:**
1. Запустить все E2E тесты: `pytest tests/e2e/ -v`
2. Если есть падения — исправить
3. Убедиться, что модули (auth/workspace/llm) работают через @task декоратор

---

### Шаг 16: Финальная проверка — все тесты

- **Файл:** —
- **Сложность:** средняя
- **Зависимости:** шаг 15
- **Ожидаемый результат:** 924+ passed / 0 failed, 44/44 E2E.

**Что сделать:**
1. Запустить все тесты: `pytest tests/ -v`
2. Проверить покрытие: `pytest tests/ --cov=pools --cov=core --cov-report=term-missing`
3. Убедиться, что нет регрессий

---

### Риски

| Риск | Вероятность | Влияние | Митигация |
|------|-------------|---------|-----------|
| SharedMemory утечка памяти при аварийном завершении | средняя | высокое | `atexit.register(cleanup)`, `signal` handlers, periodic cleanup |
| Deadlock при записи/чтении SharedMemory | низкая | высокое | Thread-safe через Lock, таймауты на операциях |
| Regression в legacy-режиме (fn._db_type) | средняя | высокое | Legacy-тесты остаются без изменений, запускать отдельно |
| WorkerManager.submit блокирует вызывающий поток | средняя | среднее | Новый submit возвращает Future, не блокирует |
| AdaptiveRouter ломает маршрутизацию | низкая | среднее | Fallback на LoadBalancer.select_worker() при ошибке |
| E2E тесты падают из-за изменений в @task | средняя | высокое | @task декоратор возвращает TaskFuture, совместимый с Future |
| Превышение лимита SharedMemory | низкая | среднее | Периодический cleanup, лимит на количество задач |

---

### Итого

| Метрика | Значение |
|---------|----------|
| Шагов | 16 |
| Новых файлов | 4 (core/shared_memory.py, pools/worker_thread_pool.py, tests/test_shared_memory.py, tests/test_worker_thread_pool.py) |
| Изменённых файлов | 7 (pools/smart_dispatcher.py, pools/worker_manager.py, pools/load_balancer.py, core/adaptive_router.py, core/task_decorator.py, core/interfaces.py, core/application.py) |
| Изменённых тестов | 4 (test_smart_dispatcher.py, test_worker_manager.py, test_load_balancer.py, + e2e проверка) |
| Сложность | высокая |
| Время | ~12-16 часов |
| Новых тестов | ~20-25 (unit + integration) |
| Существующих тестов под угрозой | ~15-20 (主要是 SmartDispatcher и WorkerManager тесты) |
| E2E под угрозой | 44 (требуется проверка) |

### Порядок выполнения

```
Шаг 1 (SharedMemoryManager) ──→ Шаг 2 (WorkerThreadPool) ──→ Шаг 3 (WorkerManager)
                                        │                           │
                                        ▼                           ▼
                                  Шаг 4 (LoadBalancer) ←──── Шаг 5 (SmartDispatcher)
                                                                   │
                                                                   ▼
                                                           Шаг 6 (AdaptiveRouter)
                                                                   │
                                                                   ▼
                                                           Шаг 7 (@task декоратор)
                                                                   │
                                                                   ▼
                                                           Шаг 8 (interfaces.py)
                                                                   │
                                                                   ▼
                                                           Шаг 9 (Application)
                                                                   │
                                                                   ▼
                                                           Шаги 10-16 (тесты)
```

### Рекомендации

1. **ADR:** Создать ADR-003 (или следующий по порядку) для фиксации нового архитектурного решения
2. **Конфиг:** Добавить новые параметры в `mia.json5`:
   - `pools.worker.thread_pool.max_threads` (по умолчанию 4)
   - `core.shared_memory.max_tasks` (по умолчанию 1000)
   - `core.shared_memory.block_size` (по умолчанию 1MB)
3. **Метрики:** Добавить новые Prometheus метрики:
   - `shared_memory_allocations_total`
   - `shared_memory_usage_bytes`
   - `worker_thread_pool_active_tasks`
   - `worker_thread_pool_submitted_total`
4. **Документация:** Обновить README с описанием новой архитектуры
5. **Мониторинг:** Добавить dashboards для мониторинга new architectures