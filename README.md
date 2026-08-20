# mia

State Manager с модульной системой. Python >= 3.11.

belle — один процесс, обёртка вокруг `Application()`. `@task` кладётся в Redis (очередь `mia`). Исполняет mia-worker:

```bash
python -m modules.worker
```

`Application()` без `dispatcher=` шлёт в Redis-очередь. Для тестов: `MIA_DISPATCH=local` (in-process).

Проект находится в активной разработке (v0.0.0).

## Quick Start

```python
from state import State

state = State()
state.load_module("sample")

result = state.api.sample.add(1, 2)  # -> 3

state.unload_module("sample")
state.shutdown()
```

Автозагрузка всех модулей из директории `modules/`:

```python
state = State()
state.load_all_modules()
```

## API Reference

### State

Точка входа. Управляет загрузкой модулей и предоставляет доступ к API.

```python
from state import State

state = State(modules_dir="modules")  # путь к модулям
```

| Метод | Описание |
|-------|----------|
| `load_module(name)` | Загрузить модуль по имени |
| `load_all_modules()` | Автосканирование и загрузка всех модулей |
| `unload_module(name)` | Выгрузить модуль |
| `shutdown()` | Корректное завершение (выгрузка всех модулей) |
| `api` | Прокси для доступа к API модулей |

### ModuleBase

Базовый класс для всех модулей. Модули наследуются от `ModuleBase` и реализуют абстрактное свойство `name`.

```python
from module_base import ModuleBase, api_method

class MyModule(ModuleBase):
    @property
    def name(self) -> str:
        return "my_module"

    @property
    def version(self) -> str:
        return "1.0.0"

    def on_load(self, state) -> None:
        """Вызывается при загрузке модуля."""
        pass

    def on_unload(self) -> None:
        """Вызывается при выгрузке модуля."""
        pass
```

| Метод/Свойство | Описание |
|----------------|----------|
| `name` | Уникальное имя модуля (abstract, обязателен) |
| `version` | Версия модуля (по умолчанию `"0.0.0"`) |
| `on_load(state)` | Инициализация при загрузке |
| `on_unload()` | Очистка ресурсов при выгрузке |

### @api_method

Декоратор для регистрации API методов модуля.

```python
@api_method
def add(self, a: int, b: int) -> int:
    return a + b

@api_method(parallel=True)
def heavy_computation(self, data: list) -> int:
    return sum(data)
```

| Параметр | По умолчанию | Описание |
|----------|--------------|----------|
| `parallel` | `False` | Если `True`, метод выполняется в отдельном потоке |

## Создание модуля

1. Создайте директорию в `modules/`:

```
modules/
  my_module/
    __init__.py
```

2. Реализуйте модуль:

```python
from module_base import ModuleBase, api_method


class MyModule(ModuleBase):
    @property
    def name(self) -> str:
        return "my_module"

    @property
    def version(self) -> str:
        return "1.0.0"

    def on_load(self, state) -> None:
        print(f"MyModule loaded")

    @api_method
    def hello(self, name: str) -> str:
        return f"Hello, {name}!"

    @api_method(parallel=True)
    def process(self, data: list) -> int:
        return sum(data)
```

3. Используйте:

```python
from state import State

state = State()
state.load_module("my_module")

print(state.api.my_module.hello("World"))  # -> "Hello, World!"
print(state.api.my_module.process([1, 2, 3]))  # -> 6
```

## Метрики

Prometheus метрики доступны через `MetricsServer` на порту `:9090/metrics`.

| Метрика | Тип | Описание |
|---------|-----|----------|
| `state_api_calls_total` | Counter | Счётчик вызовов API (labels: module, method, status) |
| `state_api_duration_seconds` | Histogram | Время выполнения API-вызова (labels: module, method) |
| `task_created_total` | Counter | Созданные задачи (labels: module, task_type) |
| `task_completed_total` | Counter | Завершённые задачи (labels: module, task_type, status) |
| `database_operations_total` | Counter | Операции Database |

Запуск сервера метрик:

```python
from metrics import MetricsServer

server = MetricsServer(port=9090)
server.start()
```

## Установка

```bash
pip install -e .
```

С метриками:

```bash
pip install -e ".[metrics]"
```

Для разработки:

```bash
pip install -e ".[dev]"
```

## Тесты

```bash
pytest
```

## Лицензия

MIT
