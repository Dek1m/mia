# План: Schema-first архитектура для mia

**Цель:** Реализовать Schema-first архитектуру с автосозданием БД/таблиц, Table Gateway, метаданными модулей.

---

## ADR-001: Schema-first архитектура

### Статус: Accepted

### Контекст
Модули mia (mail, auth, payments) должны определять свои таблицы БД. Нужен механизм:
- Автосоздания таблиц при загрузке модуля
- Удобного доступа к таблицам (`state.db.mail.outbound.insert(...)`)
- Метаданных модулей (хэш, описание, --help)

### Решение
1. **Schema-first через dict** — модули определяют схемы как dict
2. **TableGateway** — обёртка над DatabaseProvider для chaining API
3. **Авторегистрация в State** — через `__getattr__` в Application

### Почему dict, а не dataclass
- Простота: не нужно писать конвертеры
- Гибкость: можно генерировать схему динамически
- JSON-совместимость: легко сериализовать

---

## Ключевые архитектурные решения

### 1. State = Application + `__getattr__`

**Application остаётся** как Composition Root + DI + Lifecycle.

**State — это не отдельный класс.** Это `Application.__getattr__`, который делегирует доступ к модулям:

```python
# Внутри Application
class Application:
    def __getattr__(self, name: str) -> Any:
        """state.mail → services.resolve(MailProvider)"""
        # Ищем среди зарегистрированных модулей
        registry = self.services.resolve(IModuleRegistry)
        module = registry.get(name)
        if module is not None:
            return module
        
        # Ищем среди сервисов
        try:
            return self.services.resolve_by_name(name)
        except:
            raise AttributeError(f"Module '{name}' not loaded")
```

**Использование:**
```python
app = Application()
app.startup()
app.load_module("db")
app.load_module("auth")
app.load_module("mail")

# Через app (Application)
app.database  # IDatabase
app.smart_dispatcher  # SmartDispatcher

# Через app.state (Application как State)
app.state.db  # DatabaseProvider
app.state.mail  # MailProvider
app.state.auth  # AuthProvider

# chaining API
app.state.db.mail.outbound.insert(to="user@test.com")
```

**Почему не отдельный класс State:**
- Application уже является тем, что нужно (DI, lifecycle, доступ к сервисам)
- Дублирование State и Application — лишняя абстракция
- Через `__getattr__` получаем удобный API без дополнительных классов

### 2. TableGateway как обёртка над DatabaseProvider

**DatabaseProvider** — generic CRUD: `get()`, `insert()`, `update()`, `delete()`, `list()`, `count()`

**TableGateway** — обёртка, добавляющая:
- Chaining API: `db.mail.outbound.insert(...)`
- Автотипизацию по схеме
- Валидацию данных перед insert/update

```
TableGateway.insert(data)
    ↓
DatabaseProvider.insert("outbound", data)
    ↓
asyncpg pool
```

### 3. Schema-first через dict

**Модуль определяет схему как dict при on_load:**

```python
class MailModule(ModuleBase):
    def on_load(self, state):
        schemas = {
            "outbound": {
                "columns": {
                    "to": "TEXT NOT NULL",
                    "subject": "TEXT NOT NULL",
                    "body": "TEXT",
                    "created_at": "TIMESTAMPTZ DEFAULT NOW()",
                }
            }
        }
        state.db.register_schema("mail", schemas)
```

**DatabaseProvider.register_schema() создаёт таблицы:**

```python
def register_schema(self, db_name, schemas, strict=False):
    # 1. Создаём БД если нет
    # 2. Для каждой таблицы: CREATE TABLE IF NOT EXISTS
    # 3. Создаём DatabaseGateway для доступа
    self._gateways[db_name] = DatabaseGateway(self, db_name)
```

### 4. Автоматическое определение типов

```python
def _infer_type(value):
    if isinstance(value, str): return "TEXT"
    if isinstance(value, int): return "INTEGER"
    if isinstance(value, float): return "REAL"
    if isinstance(value, bool): return "BOOLEAN"
    if isinstance(value, datetime): return "TIMESTAMPTZ"
    if isinstance(value, UUID): return "UUID"
    if isinstance(value, dict): return "JSONB"
    if isinstance(value, list): return "JSONB"
    if isinstance(value, bytes): return "BYTEA"
    return "TEXT"
```

### 5. Обязательный UUID primary key

```python
# Если в схеме нет primary key — автоматически добавляем id
if not any(c.get('primary_key') for c in schema['columns'].values()):
    schema['columns'] = {
        'id': {'type': 'UUID', 'primary_key': True, 'default': 'gen_random_uuid()'},
        **schema['columns']
    }
```

---

## Часть 1: Доработка ядра mia

### Шаг 1.1: Метаданные модулей

- **Файл:** `modules_system/module_metadata.py` (создаём)
- **Что:**
  - Класс `ModuleMetadata`
  - Поля: `hash`, `state_class_name`, `main_class`, `methods`, `description`, `version`, `dependencies`
  - Метод `compute_hash(module_class)` — вычисление хэша
- **Тест:** `tests/test_module_metadata.py`

### Шаг 1.2: Module Registry — хранение метаданных

- **Файл:** `modules_system/module_registry.py` (обновляем)
- **Что:**
  - `register(name, module, metadata)` — регистрация с метаданными
  - `get_metadata(name)` — получение метаданных
  - `get_hash(name)` — получение хэша
- **Тест:** `tests/test_module_registry.py`

### Шаг 1.3: Application.__getattr__ для доступа к модулям

- **Файл:** `core/application.py` (изменяем)
- **Что:**
  - Добавляем `__getattr__(name)` → доступ к модулям как к атрибутам
  - `app.state.mail` → `app.services.resolve(MailProvider)`
  - `app.state.db` → `app.services.resolve(DatabaseProvider)`
- **Тест:** `tests/test_application_getattr.py`

### Шаг 1.4: Генерация --help

- **Файл:** `modules_system/help_generator.py` (создаём)
- **Что:**
  - Функция `generate_help(module)` → строка help
  - Парсит docstring модуля и методов
  - Форматированный вывод
- **Тест:** `tests/test_help_generator.py`

### Шаг 1.5: Интеграция с Application.startup

- **Файл:** `core/application.py` (изменяем)
- **Что:**
  - В `startup()` добавляем этап «пост-загрузка модулей»
  - После загрузки всех модулей: проверка хэшей, авторегистрация
- **Тест:** `tests/test_application_startup.py`

---

## Часть 2: Доработка модуля db

### Шаг 2.1: TableGateway — обёртка над таблицей

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

### Шаг 2.2: DatabaseGateway — обёртка над БД

- **Файл:** `modules/db/gateway.py` (добавляем)
- **Что:**
  - Класс `DatabaseGateway` с доступом к таблицам
  - `__getattr__(table_name)` → `TableGateway`
  - `register_table(name, schema)` — регистрация таблицы
  - `list_tables()` — список таблиц
  - `drop_table(name)` — удаление таблицы
- **Тест:** `tests/test_gateway.py`

### Шаг 2.3: Schema Registry — реестр схем

- **Файл:** `modules/db/schema_registry.py` (создаём)
- **Что:**
  - Класс `SchemaRegistry` для хранения схем
  - `register(db_name, table_name, schema)` — регистрация схемы
  - `get(db_name, table_name)` — получение схемы
  - `all()` — все схемы
  - `unregister(db_name)` — удаление схемы модуля
- **Тест:** `tests/test_schema_registry.py`

### Шаг 2.4: Автоопределение типов

- **Файл:** `modules/db/type_inference.py` (создаём)
- **Что:**
  - Функция `_infer_type(value)` → SQL-тип
  - Маппинг: str→TEXT, int→INTEGER, float→REAL, bool→BOOLEAN, datetime→TIMESTAMPTZ, UUID→UUID, dict→JSONB, list→JSONB, bytes→BYTEA
- **Тест:** `tests/test_type_inference.py`

### Шаг 2.5: Интеграция с DatabaseProvider

- **Файл:** `modules/db/provider.py` (изменяем)
- **Что:**
  - `register_schema(db_name, schema, strict=False)` — регистрация схемы + автосоздание таблиц
  - `unregister_schema(db_name)` — удаление схемы
  - `__getattr__(db_name)` → `DatabaseGateway`
  - `create_database(db_name)` / `drop_database(db_name)` — управление БД
  - `create_table(db_name, table_name, schema)` / `drop_table(db_name, table_name)` — управление таблицами
- **Режимы:**
  - `strict=False` (default) — CREATE TABLE IF NOT EXISTS
  - `strict=True` — ошибка если существует
  - `force=True` — DROP + CREATE
- **Тест:** `tests/test_schema_integration.py`

### Шаг 2.6: Удаление БД и таблиц

- **Файл:** `modules/db/provider.py` (добавляем методы)
- **Что:**
  - `create_database(db_name)` — создание БД
  - `drop_database(db_name)` — удаление БД (с подтверждением)
  - `create_table(db_name, table_name, schema)` — создание таблицы
  - `drop_table(db_name, table_name)` — удаление таблицы
- **Тест:** `tests/test_database_management.py`

---

## Часть 3: Создание модуля auth

### Шаг 3.1: AuthProvider — провайдер авторизации

- **Файл:** `modules/auth/provider.py` (обновляем)
- **Что:**
  - Регистрация схемы users/roles/permissions в on_load
  - CRUD пользователей через `state.db.auth.users`
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

### Шаг 3.2: Метаданные модуля auth

- **Файл:** `modules/auth/__init__.py` (обновляем)
- **Что:**
  - `MODULE_HASH` — хэш модуля
  - `STATE_CLASS_NAME = "Auth"` → `state.auth`
  - `MAIN_CLASS = "AuthProvider"`
  - `METHODS` — описание методов для --help
  - `__doc__` — описание модуля
- **Авторегистрация:** `state.register_module("auth", provider)`

### Шаг 3.3: Дефолтные роли и права

- **Файл:** `modules/auth/defaults.py` (создаём)
- **Что:**
  - Дефолтные роли: `admin`, `user`, `moderator`
  - Дефолтные права: `users:read`, `users:write`, `users:delete`, `admin:all`
  - Дефолтный администратор: `admin/admin`
- **Создание при первой загрузке** если таблицы пусты

### Шаг 3.4: Тесты auth

- **Файл:** `modules/auth/tests/test_auth.py` (обновляем)
- **Тесты:**
  - Регистрация пользователя
  - Вход/выход
  - Проверка разрешений
  - Работа с ролями
  - Автосоздание таблиц

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
  - Доступ через chaining API

### Шаг 4.2: Обновление существующих тестов

- **Файл:** `tests/test_database.py`, `tests/test_smart_dispatcher.py`, etc.
- **Обновления:** адаптация к новой архитектуре

---

## Зависимости

```
Часть 1 (ядро mia):
  Шаг 1.1 (ModuleMetadata)
    ↓
  Шаг 1.2 (Module Registry) ← зависит от 1.1
    ↓
  Шаг 1.3 (Application.__getattr__) ← зависит от 1.2
    ↓
  Шаг 1.4 (Help generator) ← параллельно с 1.3
    ↓
  Шаг 1.5 (Интеграция с startup) ← зависит от 1.1-1.4

Часть 2 (db доработка):
  Шаг 2.1 (TableGateway)
    ↓
  Шаг 2.2 (DatabaseGateway) ← зависит от 2.1
    ↓
  Шаг 2.3 (Schema Registry) ← параллельно с 2.2
    ↓
  Шаг 2.4 (Type inference) ← параллельно с 2.3
    ↓
  Шаг 2.5 (Интеграция с DatabaseProvider) ← зависит от 2.1-2.4
    ↓
  Шаг 2.6 (Удаление БД/таблиц) ← зависит от 2.5

Часть 3 (auth модуль):
  Зависит от Части 2 (db доработка)

Часть 4 (интеграция):
  Зависит от Частей 1-3
```

---

## Оценка трудозатрат

| Часть | Шаги | Сложность | Оценка |
|-------|------|-----------|--------|
| 1. ядро mia | 1.1-1.5 | средняя | 2.5 дня |
| 2. db доработка | 2.1-2.6 | высокая | 4 дня |
| 3. auth модуль | 3.1-3.4 | средняя | 2 дня |
| 4. Интеграция | 4.1-4.2 | средняя | 1 день |
| **Итого** | | | **9.5 дней** |

---

## Структура файлов

```
core/
├── application.py       (обновлён — __getattr__, пост-загрузка модулей)
├── database.py          (обновлён — register_schema, __getattr__)
├── factories.py         (обновлён)
└── ...

modules_system/
├── module_base.py       (без изменений)
├── module_registry.py   (обновлён — хранение метаданных)
├── module_metadata.py   (НОВЫЙ — ModuleMetadata)
├── help_generator.py    (НОВЫЙ — генерация --help)
└── tests/
    ├── test_module_metadata.py
    ├── test_help_generator.py
    └── test_application_getattr.py

modules/db/
├── __init__.py
├── provider.py          (обновлён — register_schema, __getattr__, CRUD БД)
├── gateway.py           (НОВЫЙ — TableGateway, DatabaseGateway)
├── schema_registry.py   (НОВЫЙ — SchemaRegistry)
├── type_inference.py    (НОВЫЙ — автоопределение типов)
├── config.py
├── validators.py
└── tests/
    ├── test_gateway.py
    ├── test_schema_registry.py
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

---

## Пример использования

```python
from mia import Application

# Создание
app = Application()

# Загрузка модулей
app.load_module("db")
app.load_module("auth")
app.load_module("mail")

# Startup (автосоздание таблиц)
app.startup()

# Доступ через app.state (Application как State)
app.state.db  # DatabaseProvider
app.state.auth  # AuthProvider
app.state.mail  # MailProvider

# Chaining API
app.state.db.mail.outbound.insert(to="user@test.com", subject="Hello")
app.state.db.auth.users.get(user_id="user:123")

# Через app напрямую
app.database  # IDatabase
app.smart_dispatcher  # SmartDispatcher

# Shutdown
app.shutdown()
```
