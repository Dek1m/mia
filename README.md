# mia

State Manager с модульной системой и multiprocessing dispatching для Python 3.10+.

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
