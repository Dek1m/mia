# Отчёт: Проверка интеграции Universal Task System

**Дата:** 2026-08-16
**Проверяющий:** Катерина (QA)
**Код:** Сона (Programmer)
**Задача:** Найти баги в реализации async bridge, @task, modules/db

---

## Таблица: сценарий → статус

| # | Сценарий | Статус | Детали |
|---|----------|--------|--------|
| 1 | Async bridge: async-функция в ThreadPool | ⚠️ FAIL | `new_event_loop().run_until_complete()` падает в running loop |
| 2 | @task с dispatcher: Task в TaskStore | ✅ PASS | Синхронные задачи корректно записываются |
| 3 | @task без dispatcher (standalone fallback) | ✅ PASS | Inline fallback работает |
| 4 | Двухфазная маршрутизация: classify → override | ✅ PASS | Classifier + AdaptiveRouter работают |
| 5 | Write-lock: async write-задачи сериализуются | ✅ PASS | write-lock корректно блокирует |
| 6 | apiproxy.call диспатчится через dispatcher | ✅ PASS | @task на call/list_api работает |
| 7 | modules/db: transaction() НЕ сломан | ❌ FAIL | @db_method ломает @asynccontextmanager |
| 8 | Метрики: threadpool_tasks_submitted_total | ✅ PASS | Инкрементируются |
| 9 | ISmartDispatcher: dispatch_async в интерфейсе | ❌ FAIL | dispatch_async отсутствует в интерфейсе |
| 10 | Regression: E2E тесты (44) | ✅ PASS | Все 44 проходят |

---

## Найденные баги

### БАГ-1 (КРИТИЧЕСКИЙ): `dispatch_async` не работает в async контексте

**Файл:** `pools/smart_dispatcher.py:173-178`
**Строки:** 173-178 (метод `_async_wrapper` внутри `dispatch_async`)

**Проблема:**
```python
def _async_wrapper() -> Any:
    loop = asyncio.new_event_loop()  # ← НОВЫЙ loop
    try:
        return loop.run_until_complete(fn(*call_args, **kwargs))  # ← RuntimeError
    finally:
        loop.close()
```

Когда `dispatch_async` вызывается из async-функции (например, `@task`-декорированной async-метода), в текущем потоке уже есть running event loop. `asyncio.new_event_loop().run_until_complete()` бросает:
```
RuntimeError: Cannot run the event loop while another loop is running
```

**Сценарий воспроизведения:**
1. Async-функция декорирована `@task(type="cpu")`
2. Вызывается из async-контекста (например, `loop.run_until_complete(async_fn(4))`)
3. `@task` wrapper → `_wrap_async` → `dispatcher.dispatch_async(task_obj, fn, 4)`
4. `dispatch_async` создаёт `_async_wrapper` и отправляет в ThreadPool
5. **FakeThreadPool** выполняет `_async_wrapper` в том же потоке → RuntimeError
6. **Реальный ThreadPool** выполняет в отдельном потоке → OK (нет running loop)

**Влияние:**
- **Продакшен (реальный ThreadPool):** Не падает, но coroutine создаётся и уничтожается в отдельном потоке — работает корректно
- **Тесты (FakeThreadPool):** RuntimeError ловится fallback в `_wrap_async`, dispatcher НЕ используется, тестируется fallback path
- **RuntimeWarning:** Coroutine `async_compute` never awaited — артефакт FakeThreadPool

**Ожидаемое:** `dispatch_async` корректно работает как из sync, так и из async контекста
**Фактическое:** В async контексте с FakeThreadPool — RuntimeError + RuntimeWarning; в реальном ThreadPool — работает (но coroutine создаётся на новом loop)

**Рекомендация Соне:** Заменить `asyncio.new_event_loop().run_until_complete()` на `asyncio.run_coroutine_threadsafe(coro, loop)` или использовать `asyncio.to_thread()` для оборачивания coroutine. Либо использовать `loop.run_in_executor()` для async-задач.

---

### БАГ-2 (КРИТИЧЕСКИЙ): `transaction()` сломан `@db_method`

**Файл:** `modules/db/provider.py:724-736`

**Проблема:**
```python
@db_method(type="transaction", timeout=30.0)  # ← оборачивает в async def wrapper
@asynccontextmanager                            # ← оборачивает в _AsyncGeneratorContextManager
async def transaction(self):
    async with self._pool.acquire() as conn:
        async with conn.transaction():
            yield conn
```

`@db_method` создаёт `async def wrapper()`, который делает `await base(*args, **kwargs)`. Но `base` — это `_AsyncGeneratorContextManager` (от `@asynccontextmanager`), а НЕ coroutine. Результат: `provider.transaction()` возвращает **coroutine**, а не async context manager.

**Сценарий воспроизведения:**
```python
async with provider.transaction() as conn:
    # TypeError: 'coroutine' object does not support the asynchronous context manager protocol
```

**Влияние:**
- `transaction()` **нигде не используется** в кодовой базе (проверено grep'ом)
- Но это блокирует использование транзакций в будущем
- CRUD-операции (get/insert/update/delete) работают через `@db_method` корректно

**Ожидаемое:** `async with provider.transaction() as conn:` работает
**Фактическое:** `TypeError: 'coroutine' object does not support the asynchronous context manager protocol`

**Рекомендация Соне:** Не декорировать `transaction()` через `@db_method`, либо создать отдельный декоратор для async context managers, который не оборачивает результат в `async def wrapper`.

---

### БАГ-3 (ВАЖНЫЙ): `ISmartDispatcher` не объявляет `dispatch_async`

**Файл:** `core/interfaces.py:163-186`

**Проблема:**
`task_decorator.py` вызывает `dispatcher.dispatch_async()`, но `ISmartDispatcher` объявляет только: `dispatch`, `acquire_lock`, `release_lock`, `metrics`.

Любая строгая реализация `ISmartDispatcher` (например, в тестах или при замене SmartDispatcher) не будет иметь `dispatch_async` → `AttributeError` в runtime.

**Ожидаемое:** `ISmartDispatcher` содержит `dispatch_async`
**Фактическое:** `dispatch_async` отсутствует в интерфейсе

**Рекомендация Соне:** Добавить `dispatch_async` в `ISmartDispatcher`:
```python
@abstractmethod
def dispatch_async(self, first: Any, *args: Any, **kwargs: Any) -> Any:
    """Асинхронная маршрутизация задачи."""
    ...
```

---

### БАГ-4 (КОСМЕТИКА): `_task_type_to_metric_key` маппит все типы в `read`

**Файл:** `pools/smart_dispatcher.py:447-459`

**Проблема:**
```python
def _task_type_to_metric_key(task_type: TaskType) -> str:
    if task_type == TaskType.AGGREGATE:
        return "aggregate"
    if task_type in (TaskType.IO, TaskType.DATABASE):
        return "read"
    if task_type == TaskType.CPU:
        return "read"     # ← CPU = read?
    if task_type == TaskType.GPU:
        return "read"     # ← GPU = read?
    if task_type == TaskType.NETWORK:
        return "read"     # ← NETWORK = read?
    return "read"
```

Все типы кроме AGGREGATE маппятся в `read`. Метрики бесполезны для различения CPU/GPU/NETWORK/DATABASE задач.

**Влияние:** Мониторинг не может определить, какие задачи нагружают CPU vs I/O

**Рекомендация Соне:** Расширить `_metrics` dict и маппинг, либо принять это как design decision (legacy совместимость).

---

### БАГ-5 (ВАЖНЫЙ): Сломанные импорты в тестах

**Файлы:**
- `tests/test_adaptive_router.py:2` — `from core.adaptive_router import HISTORY_WINDOW, P95_THRESHOLD`
- `tests/test_task_system_e2e.py:12` — `from core.adaptive_router import AdaptiveRouter, P95_THRESHOLD`

**Проблема:** После рефакторинга `AdaptiveRouter` константы `HISTORY_WINDOW` и `P95_THRESHOLD` были заменены на `MiaConfig.get_value()`, но тесты всё ещё импортируют их как имена из модуля.

**Влияние:** 2 тестовых файла не могут быть собраны (ImportError), ~50+ тестов недоступны.

**Рекомендация Соне:** Обновить импорты в тестах:
```python
# Было:
from core.adaptive_router import HISTORY_WINDOW, P95_THRESHOLD
# Стало:
from core.adaptive_router import AdaptiveRouter
# Использовать router._history_window, router._p95_threshold
```

---

### ПРОБЛЕМА-6 (ТЕСТ КАЧЕСТВА): FakeThreadPool тестирует fallback, не dispatcher

**Файл:** `tests/test_async_bridge.py:216-236` (`test_async_task_uses_dispatcher`)

**Проблема:** Тест утверждает что тестирует "dispatch через SmartDispatcher", но на самом деле тестирует **fallback inline path** (см. БАГ-1). FakeThreadPool выполняет `_async_wrapper` в том же потоке, где уже есть running loop → RuntimeError → fallback.

**Влияние:** Тесты дают ложное чувство покрытия dispatcher path для async-задач.

**Рекомендация:** Добавить тесты с `RealThreadPool` (как в `test_universal_task_integration.py`).

---

## Итоговый прогон

### Корень проекта (tests/)

| Файл | Результат |
|------|-----------|
| test_async_bridge.py | 14 passed, 1 warning |
| test_e2e.py | 44 passed |
| test_task_decorator.py | 22 passed |
| test_task_store.py | 21 passed |
| test_task.py | 18 passed |
| test_task_classifier.py | 48 passed |
| test_smart_dispatcher.py | 21 passed |
| test_universal_task_integration.py | 40 passed, 1 xfailed |
| test_backward_compat.py | 43 passed, **2 failed** (pre-existing) |
| test_provider_v2.py | 15 passed |
| test_db_method_decorator.py | 24 passed |
| test_database_v2.py | 17 passed |
| test_database.py | 8 passed |
| test_database_integration.py | 35 passed |
| test_db_e2e.py | 40 passed |
| **ИТОГО корень** | **411 passed, 1 xfailed, 2 failed (pre-existing)** |

### Модули (modules/)

| Результат | Количество |
|-----------|-----------|
| Passed | 471 |
| Failed | **9** (pre-existing, auth phase1 — datetime/MockPool) |
| Skipped | 16 (integration postgres) |

### Pre-existing баги (НЕ связаны с UTS)

- `test_backward_compat.py`: 2 failed — `asyncio.get_event_loop()` без running loop в Python 3.11
- `test_phase1_logic.py`: 9 failed — timezone-naive vs timezone-aware datetime, MockPool JOIN-limitations

---

## Вердикт

### Можно ли принимать работу?

**ЧАСТИЧНО — с оговорками.**

**Что работает:**
- ✅ Sync-задачи через SmartDispatcher — корректно
- ✅ @task decorator (sync/async) с fallback — корректно
- ✅ @task на apiproxy call/list_api — корректно
- ✅ Двухфазная маршрутизация (classify → override) — корректно
- ✅ Write-lock — корректно
- ✅ TaskStore интеграция — корректно
- ✅ E2E тесты (44) — все проходят
- ✅ Метрики — инкрементируются

**Что НЕ работает / требует доработки:**
- ❌ `dispatch_async` для async-задач в async контексте — RuntimeError с FakeThreadPool, работает с реальным ThreadPool
- ❌ `transaction()` — сломан `@db_method`
- ❌ `ISmartDispatcher` — неполный интерфейс
- ❌ 2 тестовых файла сломаны (ImportError)
- ⚠️ Метрикиlossy (все типы → "read")

**Рекомендация:** Принять с приоритетным исправлением БАГ-1 и БАГ-2. БАГ-3 и БАГ-4 — в следующем спринте.
