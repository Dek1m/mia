# SQL Schema — Universal Task System

## Назначение

Схема для хранения истории задач, агрегированной статистики и правил классификатора в рамках Universal Task System проекта **mia**.

## Таблицы

### `task_history`

История выполненных задач. Каждая запись — один запуск задачи через `SmartDispatcher`.

| Поле | Тип | Описание |
|------|-----|----------|
| `task_id` | UUID | Уникальный идентификатор задачи |
| `module_id` | TEXT | Идентификатор модуля-источника |
| `task_type` | TEXT | Тип задачи: read, write, aggregate, transaction |
| `fn_name` | TEXT | Имя функции, породившей задачу |
| `status` | TEXT | Статус: pending, classified, dispatched, running, completed, failed |
| `started_at` | TIMESTAMPTZ | Время начала выполнения |
| `completed_at` | TIMESTAMPTZ | Время завершения |
| `duration_ms` | REAL | Длительность выполнения в миллисекундах |
| `error` | TEXT | Текст ошибки (если есть) |
| `metadata` | JSONB | Произвольный контекст (имя модуля, атрибуты) |
| `created_at` | TIMESTAMPTZ | Время создания записи |

### `task_stats`

Агрегированная статистика по комбинациям `(module_id, task_type)`. Обновляется батчем через `StatsBatchWriter`.

| Поле | Тип | Описание |
|------|-----|----------|
| `module_id` | TEXT | Идентификатор модуля |
| `task_type` | TEXT | Тип задачи |
| `count` | INT | Количество выполненных задач |
| `avg_duration_ms` | REAL | Средняя длительность |
| `p95_duration_ms` | REAL | 95-й перцентиль длительности |
| `p99_duration_ms` | REAL | 99-й перцентиль длительности |
| `last_updated` | TIMESTAMPTZ | Время последнего обновления |

**Ограничение уникальности:** `UNIQUE(module_id, task_type)`

### `task_classifier_rules`

Правила каскадного классификатора задач. Загружаются в `TaskClassifier` при старте.

| Поле | Тип | Описание |
|------|-----|----------|
| `priority` | INT | Приоритет правила (меньше = выше) |
| `condition_type` | TEXT | Тип условия: `module_name`, `function_pattern`, `explicit` |
| `condition_value` | TEXT | Значение условия (имя модуля, паттерн функции, атрибут) |
| `target_type` | TEXT | Результирующий тип задачи |
| `enabled` | BOOLEAN | Включено ли правило |

## Индексы

| Индекс | Таблица | Колонки | Назначение |
|--------|---------|---------|------------|
| `idx_task_history_module_type` | `task_history` | `(module_id, task_type)` | Быстрый поиск по модулю и типу |
| `idx_task_history_created` | `task_history` | `(created_at DESC)` | Сортировка по времени, выборка последних |
| `idx_task_history_status` | `task_history` | `(status)` | Фильтрация по статусу |
| `idx_task_classifier_enabled` | `task_classifier_rules` | `(enabled)` | Быстрая загрузка активных правил |

## Применение

```bash
psql -d mia -f storage/sql/001_task_system.sql
```
