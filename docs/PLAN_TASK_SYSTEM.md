# План: Universal Task System — Ядро mia

**Цель:** Создать универсальную систему задач с каскадной классификацией, двухфазной маршрутизацией и обучением на исторических данных.

## Архитектура декораторов

```
@task           — универсальный (метаданные, retry, валидация, metrics)
    ↑
@db_method      — БД-специфичный (кеш, lock) — самостоятельный декоратор

Порядок: функция → @db_method → @task → SmartDispatcher
```

---

## Шаг 1: Класс Task

- **Файл:** `core/task.py` (создаём)
- **Содержимое:**
  - `TaskID` — UUID (auto-generated)
  - `module_id` — str (откуда пришла задача)
  - `task_type` — str (read/write/aggregate/transaction)
  - `status` — enum (pending → classified → dispatched → running → completed/failed)
  - `created_at`, `started_at`, `completed_at` — таймеры
  - `payload` — dict (аргументы задачи)
  - `metadata` — dict (контекст: имя модуля, функция, атрибуты)
  - `result` — Any (результат выполнения)
  - `error` — str | None
- **Тест:** `tests/test_task.py`

---

## Шаг 2: SQL-схема

- **Файл:** `storage/sql/001_task_system.sql` (создаём)
- **Таблицы:**
  - `task_history` — история выполненных задач
  - `task_stats` — агрегированная статистика
  - `task_classifier_rules` — правила классификатора
- **Индексы:** `(module_id, task_type)`, `(created_at)`, `(status)`
- **Тест:** SQL-интеграционные тесты

---

## Шаг 3: TaskStore (ring buffer + async flush)

- **Файл:** `core/task_store.py` (создаём)
- **Логика:**
  - Ring buffer на 25K строк (`collections.deque` с `maxlen=25000`)
  - `add(task)` — добавить в ring buffer
  - `get(task_id)` — поиск в памяти
  - `flush()` — async метод, записывает батч в PostgreSQL
  - `start_flusher(interval=5.0)` — фоновая задача
  - `stop_flusher()` — корректная остановка
- **Тест:** `tests/test_task_store.py`

---

## Шаг 4: TaskClassifier (каскад правил)

- **Файл:** `core/task_classifier.py` (создаём)
- **Каскад:**
  1. Явный атрибут (`._task_type` → тип)
  2. Явный атрибут (`._db_type` → тип, обратная совместимость)
  3. Имя модуля (`db.*` → read/write)
  4. Имя функции (`get_*` → read, `insert_*` → write)
  5. Fallback → default=read
- **API:**
  - `classify(task: Task) -> str`
  - `add_rule(rule: ClassifierRule)`
  - `load_rules()`
- **Тест:** `tests/test_task_classifier.py`

---

## Шаг 5: AdaptiveRouter (обучение)

- **Файл:** `core/adaptive_router.py` (создаём)
- **Логика:**
  - Собирает статистику из `task_stats`
  - Определяет оптимальный pool на основе historical duration
  - `override(task: Task) -> str | None`
  - Периодический пересчёт
- **Тест:** `tests/test_adaptive_router.py`

---

## Шаг 6: SmartDispatcher (обновлённый)

- **Файл:** `pools/smart_dispatcher.py` (изменяем)
- **Изменения:**
  - Фаза 1: `TaskClassifier.classify()` → определяет тип
  - Фаза 2: `AdaptiveRouter.override()` → корректирует тип
  - Интеграция с `TaskStore` и `StatsBatchWriter`
  - Обновление метрик Prometheus
- **Тест:** `tests/test_smart_dispatcher_v2.py`

---

## Шаг 7: @task (универсальный декоратор)

- **Файл:** `core/task.py` (создаём, в том же файле что и класс Task)
- **Логика:**
  - Атрибуты: `_task_type`, `_task_timeout`, `_task_module_id`, `_task_metrics`
  - Автоматически создаёт `Task` из аргументов функции
  - Retry, timeout, валидация
  - **Не зависит** от `@db_method` — это внешний декоратор
- **Пример использования:**
  ```python
  @task(type="cpu", timeout=10.0, retry=3)
  def heavy_computation(data):
      ...
  ```
- **Тест:** `tests/test_task_decorator.py`

---

## Шаг 8: StatsBatchWriter

- **Файл:** `core/stats_batch_writer.py` (создаём)
- **Логика:**
  - Буфер (list, max 1000)
  - `add(task: Task)` — добавить в буфер
  - `flush()` — async метод, batch INSERT в `task_history` + UPDATE `task_stats`
  - `start(interval=10.0)` — фоновая задача
  - `stop()` — корректная остановка
- **Тест:** `tests/test_stats_batch_writer.py`

---

## Шаг 9: Метрики Prometheus

- **Файл:** `monitoring/metrics.py` (изменяем)
- **Новые метрики:**
  - `task_created_total` (Counter) — labels: module_id, task_type
  - `task_completed_total` (Counter) — labels: module_id, task_type, status
  - `task_duration_seconds` (Histogram) — labels: module_id, task_type
  - `task_store_size` (Gauge)
  - `task_store_flush_total` (Counter)
  - `task_classifier_rules_total` (Gauge)
  - `task_adaptive_overrides_total` (Counter)

---

## Шаг 10: Интеграция с Database facade

- **Файл:** `core/database.py` (изменяем)
- **Изменения:**
  - Инъекция `TaskStore` и `StatsBatchWriter`
  - Каждая операция оборачивается в `Task`
  - Запись результата в `StatsBatchWriter`
- **Тест:** `tests/test_database_v2.py`

---

## Шаг 11: E2E тесты

- **Файл:** `tests/test_task_system_e2e.py` (создаём)
- **Сценарии:**
  - Полный цикл: Task → classify → dispatch → execute → stats
  - Adaptive override
  - Overflow ring buffer → flush
  - Конкурентное добавление задач

---

## Зависимости между шагами

```
Шаг 1 (Task)
  ↓
Шаг 2 (SQL-схема) ← параллельно с Шагом 1
  ↓
Шаг 3 (TaskStore) ← зависит от Шагов 1, 2
  ↓
Шаг 4 (TaskClassifier) ← зависит от Шагов 1, 2
  ↓
Шаг 5 (AdaptiveRouter) ← зависит от Шагов 1, 2, 3
  ↓
Шаг 6 (SmartDispatcher) ← зависит от Шагов 1-5
  ↓
Шаг 7 (@task декоратор) ← зависит от Шагов 1, 6
  ↓
Шаг 8 (StatsBatchWriter) ← зависит от Шагов 1, 2
  ↓
Шаг 9 (Метрики) ← зависит от всех предыдущих
  ↓
Шаг 10 (Database integration) ← зависит от Шагов 3, 8
  ↓
Шаг 11 (E2E тесты)
```

---

## Оценка трудозатрат

| Шаг | Описание | Сложность | Оценка |
|-----|----------|-----------|--------|
| 1 | Класс Task | низкая | 0.5 дня |
| 2 | SQL-схема | низкая | 0.5 дня |
| 3 | TaskStore | средняя | 1 день |
| 4 | TaskClassifier | средняя | 1 день |
| 5 | AdaptiveRouter | высокая | 1.5 дня |
| 6 | SmartDispatcher | высокая | 1.5 дня |
| 7 | @task декоратор | средняя | 0.5 дня |
| 8 | StatsBatchWriter | средняя | 0.5 дня |
| 9 | Метрики | низкая | 0.5 дня |
| 10 | Database integration | средняя | 1 день |
| 11 | E2E тесты | высокая | 1 день |
| **Итого** | | | **9.5 дней** |
