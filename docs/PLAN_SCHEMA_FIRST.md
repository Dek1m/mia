# План: Доработка ядра mia + модуля db + модуля auth

**Цель:** Реализовать Schema-first архитектуру с автосозданием БД/таблиц, Table Gateway, метаданными модулей и авторегистрацией в State.

---

## Ключевые фичи

1. **Schema-first** — модули предоставляют схемы, db создаёт таблицы
2. **Table Gateway** — доступ к таблицам как к классам (`state.db.mail.outbound.insert(...)`)
3. **Автосоздание** — CREATE DATABASE/TABLE IF NOT EXISTS
4. **Строгий режим** — strict=True → ошибка если существует
5. **Автоматическое определение типов** — по значению определяет SQL-тип
6. **Обязательный UUID primary key** — автоматически если нет
7. **Метаданные модулей** — хэш, описание, методы для --help
8. **Авторегистрация в State** — `state.mail`, `state.auth`

---

## Часть 0: Доработка ядра mia

### Шаг 0.1: ModuleMetadata — система метаданных

- **Файл:** `modules_system/module_metadata.py` (создаём)
- **Что:**
  - Класс `ModuleMetadata` для хранения метаданных модуля
  - Поля:
    - `hash` — хэш модуля (SHA256 от исходного кода)
    - `state_class_name` — имя класса в State (например, "Mail" → state.mail)
    - `main_class` — имя основного класса (например, "MailProvider")
    - `methods` — dict с описаниями методов (для --help)
    - `description` — описание модуля из docstring
    - `version` — версия модуля
    - `dependencies` — список зависимостей
  - Метод `compute_hash(module_class)` — вычисление хэша
- **Тест:** `tests/test_module_metadata.py`

### Шаг 0.2: Module Registry — реестр модулей

- **Файл:** `modules_system/module_registry.py` (обновляем)
- **Что:**
  - Добавляем хранение метаданных для каждого модуля
  - `register(name, module, metadata)` — регистрация с метаданными
  - `get_metadata(name)` — получение метаданных
  - `get_hash(name)` — получение хэша
  - `list_all()` — список всех модулей с метаданными
- **Тест:** `tests/test_module_registry.py`

### Шаг 0.3: Автоматическая регистрация в State

- **Файл:** `core/application.py` (изменяем)
- **Что:**
  - При загрузке модуля проверяем `STATE_CLASS_NAME`
  - Если есть — создаём базовый класс dynamically:
    ```python
    if hasattr(module, 'STATE_CLASS_NAME'):
        class_name = module.STATE_CLASS_NAME
        base_class = type(class_name, (), {
            '__doc__': module.__doc__,
            '_module': module,
            '_provider': None,
        })
        setattr(state, class_name.lower(), base_class())
    ```
  - Регистрируем провайдер: `state.{class_name.lower()}._provider = provider`
- **Тест:** `tests/test_auto_registration.py`

### Шаг 0.4: State с поддержкой динамических атрибутов

- **Файл:** `core/state.py` (создаём или обновляем)
- **Что:**
  - Класс `State` с поддержкой `__getattr__`
  - Хранит зарегистрированные модули
  - `__getattr__(name)` → возвращает экземпляр модуля илиraises AttributeError
  - `register_module(name, instance)` — регистрация модуля
  - `modules` — реестр всех модулей
- **Тест:** `tests/test_state.py`

### Шаг 0.5: Генерация --help

- **Файл:** `modules_system/help_generator.py` (создаём)
- **Что:**
  - Функция `generate_help(module)` → строка help
  - Парсит docstring модуля и методов
  - Форматирует вывод:
    ```
    Module: MailModule
    Version: 1.0.0
    
    Модуль почты для Mia Framework.
    
    Methods:
      send — Отправить письмо.
      get_inbox — Получить входящие.
    ```
  - Функция `generate_help_all()` — help для всех модулей
- **Тест:** `tests/test_help_generator.py`

### Шаг 0.6: Интеграция с Application.startup

- **Файл:** `core/application.py` (изменяем)
- **Что:**
  - В `startup()` добавляем этап «пост-загрузка модулей»
  - После загрузки всех модулей:
    1. Проверяем хэши
    2. Автоматически регистрируем в State
    3. Генерируем --help если нужно
- **Тест:** `tests/test_application_startup.py`

---

## Часть 1: Доработка модуля db

### Шаг 1.1: TableGateway — обёртка над таблицей

- **Файл:** `modules/db/gateway.py` (создаём)
- **Что:**
  - Класс `TableGateway` с CRUD методами
  - `insert(**kwargs)` — вставка с автотипизацией
  - `get(id)` — получение по UUID
  - `update(id, **kwargs)` — обновление
  - `delete(id)` — удаление
  - `list(filters, limit, offset)` — список с фильтрами
  - `count(filters)` — подсчёт
  - `exists(id)` — проверка существования
- **Автоопределение типов:** `_infer_type(value)` → SQL-тип по значению
- **Обязательный UUID:** автоматически добавляет `id UUID PRIMARY KEY DEFAULT gen_random_uuid()`
- **Тест:** `tests/test_gateway.py`

### Шаг 1.2: DatabaseGateway — обёртка над БД

- **Файл:** `modules/db/gateway.py` (добавляем)
- **Что:**
  - Класс `DatabaseGateway` с доступом к таблицам
  - `__getattr__(table_name)` → `TableGateway`
  - `register_table(name, schema)` — регистрация таблицы
  - `list_tables()` — список таблиц
  - `drop_table(name)` — удаление таблицы
- **Тест:** `tests/test_gateway.py`

### Шаг 1.3: Schema Registry — реестр схем

- **Файл:** `modules/db/schema_registry.py` (создаём)
- **Что:**
  - Класс `SchemaRegistry` для хранения схем
  - `register(db_name, table_name, schema)` — регистрация схемы
  - `get(db_name, table_name)` — получение схемы
  - `all()` — все схемы
  - `unregister(db_name)` — удаление схемы модуля
- **Валидация схемы:** проверка обязательных полей, типов
- **Тест:** `tests/test_schema_registry.py`

### Шаг 1.4: Migration Engine — движок миграций

- **Файл:** `modules/db/migrations.py` (создаём)
- **Что:**
  - Класс `MigrationEngine` для CREATE/ALTER TABLE
  - `create_database(db_name)` — CREATE DATABASE IF NOT EXISTS
  - `drop_database(db_name)` — DROP DATABASE IF EXISTS
  - `create_table(db_name, table_name, schema)` — CREATE TABLE IF NOT EXISTS
  - `drop_table(db_name, table_name)` — DROP TABLE IF EXISTS
  - `add_column(db_name, table_name, column, type)` — ALTER TABLE ADD COLUMN
  - `drop_column(db_name, table_name, column)` — ALTER TABLE DROP COLUMN
  - `alter_column(db_name, table_name, column, type)` — ALTER TABLE ALTER COLUMN
  - `table_exists(db_name, table_name)` — проверка existence
  - `database_exists(db_name)` — проверка existence
- **Сравнение схем:** `diff(current, target)` → список миграций
- **Тест:** `tests/test_migrations.py`

### Шаг 1.5: Интеграция с DatabaseProvider

- **Файл:** `modules/db/provider.py` (изменяем)
- **Что:**
  - `register_schema(db_name, schema, strict=False)` — регистрация схемы
  - `unregister_schema(db_name)` — удаление схемы
  - `__getattr__(db_name)` → `DatabaseGateway`
  - При регистрации: проверка → миграция → создание gateway
- **Режимы:**
  - `strict=False` (default) — автосоздание
  - `strict=True` — ошибка если существует
  - `force=True` — DROP + CREATE
- **Тест:** `tests/test_schema_integration.py`

### Шаг 1.6: Автоопределение типов

- **Файл:** `modules/db/type_inference.py` (создаём)
- **Что:**
  - Функция `_infer_type(value)` → SQL-тип
  - `str` → `TEXT`
  - `int` → `INTEGER`
  - `float` → `REAL`
  - `bool` → `BOOLEAN`
  - `datetime` → `TIMESTAMPTZ`
  - `UUID` → `UUID`
  - `dict` → `JSONB`
  - `bytes` → `BYTEA`
  - `list` → `JSONB`
- **Тест:** `tests/test_type_inference.py`

### Шаг 1.7: Удаление БД и таблиц

- **Файл:** `modules/db/provider.py` (добавляем методы)
- **Что:**
  - `create_database(db_name)` — создание БД
  - `drop_database(db_name)` — удаление БД (с подтверждением)
  - `create_table(db_name, table_name, schema)` — создание таблицы
  - `drop_table(db_name, table_name)` — удаление таблицы
- **Тест:** `tests/test_database_management.py`

---

## Часть 2: Создание модуля auth

### Шаг 2.1: AuthProvider — провайдер авторизации

- **Файл:** `modules/auth/provider.py` (обновляем)
- **Что:**
  - Регистрация схемы users/roles/permissions в on_load
  - CRUD пользователей через state.db.auth.users
  - Аутентификация (login/logout)
  - Авторизация (RBAC)
  - JWT токены
  - Шифрование паролей
- **Схема:**
  - `users` (id UUID, username TEXT, password_hash TEXT, email TEXT, is_active BOOLEAN)
  - `roles` (id UUID, name TEXT, description TEXT)
  - `permissions` (id UUID, name TEXT, description TEXT)
  - `user_roles` (user_id UUID, role_id UUID)
  - `role_permissions` (role_id UUID, permission_id UUID)
- **Тест:** `tests/test_auth.py`

### Шаг 2.2: Метаданные модуля auth

- **Файл:** `modules/auth/__init__.py` (обновляем)
- **Что:**
  - `MODULE_HASH` — хэш модуля
  - `STATE_CLASS_NAME = "Auth"` → `state.auth`
  - `MAIN_CLASS = "AuthProvider"`
  - `METHODS` — описание методов для --help
  - `__doc__` — описание модуля
- **Авторегистрация:** `state.register_module("auth", provider)`

### Шаг 2.3: Дефолтные роли и права

- **Файл:** `modules/auth/defaults.py` (создаём)
- **Что:**
  - Дефолтные роли: `admin`, `user`, `moderator`
  - Дефолтные права: `users:read`, `users:write`, `users:delete`, `admin:all`
  - Дефолтный администратор: `admin/admin`
- **Создание при первой загрузке** если таблицы пусты

### Шаг 2.4: Тесты auth

- **Файл:** `modules/auth/tests/test_auth.py` (обновляем)
- **Тесты:**
  - Регистрация пользователя
  - Вход/выход
  - Проверка разрешений
  - Работа с ролями
  - Автосоздание таблиц

---

## Часть 3: Метаданные модулей

### Шаг 3.1: Module Metadata — система метаданных

- **Файл:** `modules_system/module_metadata.py` (создаём)
- **Что:**
  - Класс `ModuleMetadata` для хранения метаданных
  - `hash` — хэш модуля
  - `state_class_name` — имя класса в State
  - `main_class` — имя основного класса
  - `methods` — описание методов
  - `description` — описание модуля
- **Тест:** `tests/test_module_metadata.py`

### Шаг 3.2: Автоматическая регистрация в State

- **Файл:** `core/application.py` (изменяем)
- **Что:**
  - При загрузке модуля проверяем `STATE_CLASS_NAME`
  - Если есть — создаём базовый класс dynamically
  - Регистрируем в State: `setattr(state, class_name.lower(), instance)`
- **Тест:** `tests/test_auto_registration.py`

### Шаг 3.3: Генерация --help

- **Файл:** `modules_system/help_generator.py` (создаём)
- **Что:**
  - Функция `generate_help(module)` → строка help
  - Парсинг docstring модуля и методов
  - Форматированный вывод
- **Тест:** `tests/test_help_generator.py`

---

## Часть 4: Интеграция и тесты

### Шаг 4.1: Интеграционные тесты

- **Файл:** `tests/test_schema_first_e2e.py` (создаём)
- **Сценарии:**
  - MailModule → register_schema → CREATE TABLE → insert/select
  - AuthModule → register_schema → CREATE TABLE → create_user/login
  - Strict mode → ошибка если существует
  - Автоопределение типов
  - Удаление БД/таблиц

### Шаг 4.2: Обновление существующих тестов

- **Файл:** `tests/test_database.py`, `tests/test_smart_dispatcher.py`, etc.
- **Обновления:** адаптация к новой архитектуре

---

## Зависимости

```
Часть 1 (db доработка):
  Шаг 1.1 (TableGateway)
    ↓
  Шаг 1.2 (DatabaseGateway) ← зависит от 1.1
    ↓
  Шаг 1.3 (Schema Registry) ← параллельно с 1.2
    ↓
  Шаг 1.4 (Migration Engine) ← зависит от 1.3
    ↓
  Шаг 1.5 (Интеграция с DatabaseProvider) ← зависит от 1.1-1.4
    ↓
  Шаг 1.6 (Автоопределение типов) ← параллельно с 1.5
    ↓
  Шаг 1.7 (Удаление БД/таблиц) ← зависит от 1.4

Часть 2 (auth модуль):
  Зависит от Части 1 (db доработка)

Часть 3 (метаданные модулей):
  Зависит от Части 1 (db доработка)

Часть 4 (интеграция):
  Зависит от Частей 1-3
```

---

## Оценка трудозатрат

| Часть | Шаги | Сложность | Оценка |
|-------|------|-----------|--------|
| 0. ядро mia | 0.1-0.6 | высокая | 3 дня |
| 1. db доработка | 1.1-1.7 | высокая | 4 дня |
| 2. auth модуль | 2.1-2.4 | средняя | 2 дня |
| 3. Интеграция | 3.1-3.2 | средняя | 1 день |
| **Итого** | | | **10 дней** |

---

## Структура файлов

```
core/
├── application.py       (обновлён — пост-загрузка модулей)
├── state.py             (НОВЫЙ или обновлён — динамические атрибуты)
├── database.py          (обновлён — register_schema, __getattr__)
└── factories.py         (обновлён)

modules_system/
├── module_base.py       (без изменений)
├── module_registry.py   (обновлён — хранение метаданных)
├── module_metadata.py   (НОВЫЙ — ModuleMetadata)
├── help_generator.py    (НОВЫЙ — генерация --help)
└── tests/
    ├── test_module_metadata.py
    ├── test_help_generator.py
    ├── test_auto_registration.py
    └── test_state.py

modules/db/
├── __init__.py
├── provider.py          (обновлён — register_schema, __getattr__)
├── gateway.py           (НОВЫЙ — TableGateway, DatabaseGateway)
├── schema_registry.py   (НОВЫЙ — SchemaRegistry)
├── migrations.py        (НОВЫЙ — MigrationEngine)
├── type_inference.py    (НОВЫЙ — автоопределение типов)
├── config.py
├── validators.py
└── tests/
    ├── test_gateway.py
    ├── test_schema_registry.py
    ├── test_migrations.py
    ├── test_type_inference.py
    ├── test_database_management.py
    └── test_schema_integration.py

modules/auth/
├── __init__.py          (обновлён — метаданные, авторегистрация)
├── provider.py          (обновлён — регистрация схемы в on_load)
├── config.py
├── validators.py
├── defaults.py          (НОВЫЙ — дефолтные роли/права)
└── tests/
    └── test_auth.py

tests/
└── test_schema_first_e2e.py (НОВЫЙ — интеграционные тесты)
```
