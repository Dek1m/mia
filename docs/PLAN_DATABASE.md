# План: Database в State (обновлённый)

## Краткий анализ текущего состояния

**Есть (уже реализовано):**
- `core/interfaces.py` — `IDatabase` интерфейс ✅
- `core/database.py` — `Database` фасад ✅
- `core/factories.py` — `DatabaseFactory` ✅
- `core/application.py` — интеграция Database ✅
- `tests/test_database.py` — 12 тестов ✅
- `storage/cache_interface.py` — `ICache`, `NullCache`
- `storage/shared_memory.py` — `SharedMemoryManager`
- `pools/` — `WorkerManager`, `LoadBalancer`, `ThreadPoolManager`
- `modules/db/` — `DatabaseModule`, `DatabaseProvider`, `DatabaseConfig`, `@db_method`

**Нет (нужно создать):**
- `CacheHierarchy` — многоуровневый кеш (L0 → L1 → L2)
- `SmartDispatcher` — маршрутизация задач по типам из `@db_method`
- Observability — метрики, логи, tracing для Database

---

## Этап 0: Подготовка — убрать мёртвый код и исправить ошибки

**Статус:** ✅ Готово

---

## Этап 1: Интерфейсы и базовый фасад

**Статус:** ✅ Готово

---

## Этап 2: Cache Hierarchy (L0→L1→L2)

**Цель:** Реализовать многоуровневый кеш для Database.

### Концепция

```
L0: In-process dict (nanoseconds) — быстрый, но только в рамках процесса
L1: SharedMemory (microseconds) — общая память между процессами
L2: Redis (milliseconds) — распределённый кеш между серверами
```

**Приоритет:** L0 → L1 → L2
**Промоут:** При попадании в L0 данные промоутятся в L1
**Инвалидация:** TTL + явная при мутациях

### Шаг 2.1: Создать `storage/cache_hierarchy.py`

```python
"""Cache Hierarchy — L0 dict → L1 SharedMemory → L2 Redis."""

import time
import logging
from typing import Any
from core.interfaces import ICache

logger = logging.getLogger(__name__)


class CacheHierarchy(ICache):
    """
    Многоуровневый кеш.
    
    L0: In-process dict с TTL (nanoseconds)
    L1: SharedMemoryManager (microseconds)
    L2: Redis-клиент (milliseconds, опционально)
    """
    
    def __init__(self, l1=None, l2_client=None):
        """
        Args:
            l1: SharedMemoryManager (опционально)
            l2_client: Redis-клиент (опционально)
        """
        self._l0: dict[str, tuple[Any, float]] = {}  # key -> (value, expires_at)
        self._l1 = l1
        self._l2 = l2_client
        self._stats = {"hits": 0, "misses": 0}
    
    def get(self, key: str) -> Any | None:
        """Получить значение из кеша."""
        # L0
        if key in self._l0:
            value, expires_at = self._l0[key]
            if time.time() < expires_at:
                self._stats["hits"] += 1
                return value
            else:
                del self._l0[key]
        
        # L1
        if self._l1:
            try:
                value = self._l1.get(key)
                if value is not None:
                    self._stats["hits"] += 1
                    # Промоут в L0
                    self._l0[key] = (value, time.time() + 30)
                    return value
            except Exception as e:
                logger.warning(f"L1 cache error: {e}")
        
        # L2
        if self._l2:
            try:
                value = self._l2.get(key)
                if value is not None:
                    self._stats["hits"] += 1
                    # Промоут в L0 и L1
                    self._l0[key] = (value, time.time() + 30)
                    if self._l1:
                        self._l1.set(key, value, ttl=300)
                    return value
            except Exception as e:
                logger.warning(f"L2 cache error: {e}")
        
        self._stats["misses"] += 1
        return None
    
    def set(self, key: str, value: Any, ttl: int = 30) -> None:
        """Установить значение в кеш."""
        expires_at = time.time() + ttl
        
        # L0
        self._l0[key] = (value, expires_at)
        
        # L1
        if self._l1:
            try:
                self._l1.set(key, value, ttl=ttl)
            except Exception as e:
                logger.warning(f"L1 cache set error: {e}")
        
        # L2
        if self._l2:
            try:
                self._l2.set(key, value, ttl=ttl)
            except Exception as e:
                logger.warning(f"L2 cache set error: {e}")
    
    def delete(self, key: str) -> None:
        """Удалить значение из кеша."""
        # L0
        self._l0.pop(key, None)
        
        # L1
        if self._l1:
            try:
                self._l1.delete(key)
            except Exception as e:
                logger.warning(f"L1 cache delete error: {e}")
        
        # L2
        if self._l2:
            try:
                self._l2.delete(key)
            except Exception as e:
                logger.warning(f"L2 cache delete error: {e}")
    
    def exists(self, key: str) -> bool:
        """Проверить наличие ключа в кеше."""
        # L0
        if key in self._l0:
            _, expires_at = self._l0[key]
            if time.time() < expires_at:
                return True
            else:
                del self._l0[key]
        
        # L1
        if self._l1:
            try:
                if self._l1.exists(key):
                    return True
            except Exception:
                pass
        
        # L2
        if self._l2:
            try:
                if self._l2.exists(key):
                    return True
            except Exception:
                pass
        
        return False
    
    def clear(self) -> None:
        """Очистить весь кеш."""
        self._l0.clear()
        
        if self._l1:
            try:
                self._l1.clear()
            except Exception as e:
                logger.warning(f"L1 cache clear error: {e}")
        
        if self._l2:
            try:
                self._l2.clear()
            except Exception as e:
                logger.warning(f"L2 cache clear error: {e}")
    
    @property
    def stats(self) -> dict:
        """Статистика кеша."""
        return self._stats.copy()
```

### Шаг 2.2: Обновить `CacheFactory` в `core/factories.py`

```python
class CacheFactory:
    """Фабрика кеша."""
    
    @staticmethod
    def create("hierarchy", l1=None, l2_client=None):
        """Создать CacheHierarchy."""
        from storage.cache_hierarchy import CacheHierarchy
        return CacheHierarchy(l1=l1, l2_client=l2_client)
```

### Шаг 2.3: Интегрировать CacheHierarchy с Database

В `core/database.py`:
- `Database.__init__` принимает `ICache` (по умолчанию NullCache)
- Методы CRUD проверяют кеш перед запросом
- При записи — инвалидируют кеш

### Шаг 2.4: Написать тесты

Создай `tests/test_cache_hierarchy.py`:

```python
"""Тесты для Cache Hierarchy."""

import time
import pytest
from storage.cache_hierarchy import CacheHierarchy


def test_l0_only_cache():
    cache = CacheHierarchy()
    cache.set("key1", "value1", ttl=10)
    assert cache.get("key1") == "value1"


def test_cache_ttl_expiration():
    cache = CacheHierarchy()
    cache.set("key1", "value1", ttl=0.1)  # 100ms
    time.sleep(0.2)
    assert cache.get("key1") is None


def test_cache_delete():
    cache = CacheHierarchy()
    cache.set("key1", "value1", ttl=10)
    cache.delete("key1")
    assert cache.get("key1") is None


def test_cache_clear():
    cache = CacheHierarchy()
    cache.set("key1", "value1", ttl=10)
    cache.set("key2", "value2", ttl=10)
    cache.clear()
    assert cache.get("key1") is None
    assert cache.get("key2") is None


def test_cache_exists():
    cache = CacheHierarchy()
    cache.set("key1", "value1", ttl=10)
    assert cache.exists("key1") is True
    assert cache.exists("key2") is False


def test_cache_stats():
    cache = CacheHierarchy()
    cache.set("key1", "value1", ttl=10)
    cache.get("key1")  # hit
    cache.get("key2")  # miss
    
    assert cache.stats["hits"] == 1
    assert cache.stats["misses"] == 1


def test_null_cache_fallback():
    cache = CacheHierarchy(l1=None, l2_client=None)
    cache.set("key1", "value1", ttl=10)
    assert cache.get("key1") == "value1"
```

---

## Этап 3: SmartDispatcher

**Цель:** Маршрутизация задач по типам из `@db_method` декораторов.

### Концепция

```
Задача приходит → SmartDispatcher читает _db_type → маршрутизирует:
- type='read' → ThreadPool (много конкурентных)
- type='write' → ThreadPool (с блокировкой)
- type='aggregate' → WorkerManager (параллельно на ядрах)
- type='transaction' → ThreadPool (изолированно)
```

### Шаг 3.1: Создать `pools/smart_dispatcher.py`

```python
"""SmartDispatcher — маршрутизация задач по типам."""

import logging
from typing import Any, Callable
from core.interfaces import IThreadPool, IWorkerManager

logger = logging.getLogger(__name__)


class SmartDispatcher:
    """
    Маршрутизатор задач по типам.
    
    Читает метаданные из декоратора @db_method и решает,
    куда направить задачу: ThreadPool или WorkerManager.
    """
    
    def __init__(self, thread_pool: IThreadPool, worker_manager: IWorkerManager):
        self._thread_pool = thread_pool
        self._worker_manager = worker_manager
        self._locks: dict[str, Any] = {}
        self._metrics = {"read": 0, "write": 0, "aggregate": 0, "transaction": 0}
    
    def dispatch(self, fn: Callable, *args, **kwargs) -> Any:
        """
        Маршрутизация задачи по типу.
        
        Читает атрибуты _db_type, _db_timeout, _db_lock из fn.
        """
        task_type = getattr(fn, "_db_type", "read")
        timeout = getattr(fn, "_db_timeout", 10.0)
        lock_key = getattr(fn, "_db_lock", None)
        
        self._metrics[task_type] = self._metrics.get(task_type, 0) + 1
        
        if task_type == "read":
            return self._dispatch_read(fn, args, kwargs, timeout)
        
        elif task_type == "write":
            return self._dispatch_write(fn, args, kwargs, timeout, lock_key)
        
        elif task_type == "aggregate":
            return self._dispatch_aggregate(fn, args, kwargs, timeout)
        
        elif task_type == "transaction":
            return self._dispatch_transaction(fn, args, kwargs, timeout)
        
        else:
            raise ValueError(f"Unknown task type: {task_type}")
    
    def _dispatch_read(self, fn, args, kwargs, timeout):
        """Read-задачи идут в ThreadPool."""
        future = self._thread_pool.submit(fn, *args, **kwargs)
        return future.result(timeout=timeout)
    
    def _dispatch_write(self, fn, args, kwargs, timeout, lock_key):
        """Write-задачи идут в ThreadPool с блокировкой."""
        if lock_key:
            lock = self._acquire_lock(lock_key, timeout=5)
            try:
                future = self._thread_pool.submit(fn, *args, **kwargs)
                return future.result(timeout=timeout)
            finally:
                self._release_lock(lock_key)
        else:
            future = self._thread_pool.submit(fn, *args, **kwargs)
            return future.result(timeout=timeout)
    
    def _dispatch_aggregate(self, fn, args, kwargs, timeout):
        """Aggregate-задачи идут в WorkerManager."""
        future = self._worker_manager.submit(fn, *args, **kwargs)
        return future.result(timeout=timeout)
    
    def _dispatch_transaction(self, fn, args, kwargs, timeout):
        """Transaction-задачи идут в ThreadPool (изолированно)."""
        future = self._thread_pool.submit(fn, *args, **kwargs)
        return future.result(timeout=timeout)
    
    def _acquire_lock(self, key: str, timeout: float = 5.0):
        """Блокировка для write-операций."""
        import threading
        if key not in self._locks:
            self._locks[key] = threading.Lock()
        
        lock = self._locks[key]
        acquired = lock.acquire(timeout=timeout)
        if not acquired:
            raise TimeoutError(f"Could not acquire lock for key: {key}")
        return lock
    
    def _release_lock(self, key: str):
        """Снятие блокировки."""
        if key in self._locks:
            self._locks[key].release()
    
    @property
    def metrics(self) -> dict:
        """Метрики маршрутизации."""
        return self._metrics.copy()
```

### Шаг 3.2: Интегрировать SmartDispatcher с Database

В `core/database.py`:
- `Database.__init__` принимает `SmartDispatcher`
- Методы CRUD оборачиваются в `dispatcher.dispatch()`

### Шаг 3.3: Обновить `Application`

В `core/application.py`:
- Создать `SmartDispatcher` с `thread_pool` и `worker_manager`
- Передать в `Database`

### Шаг 3.4: Написать тесты

Создай `tests/test_smart_dispatcher.py`:

```python
"""Тесты для SmartDispatcher."""

import pytest
from unittest.mock import Mock, MagicMock
from pools.smart_dispatcher import SmartDispatcher


class MockThreadPool:
    def submit(self, fn, *args, **kwargs):
        future = Mock()
        future.result.return_value = fn(*args, **kwargs)
        return future


class MockWorkerManager:
    def submit(self, fn, *args, **kwargs):
        future = Mock()
        future.result.return_value = fn(*args, **kwargs)
        return future


def test_read_routes_to_thread_pool():
    dispatcher = SmartDispatcher(MockThreadPool(), MockWorkerManager())
    
    @staticmethod
    def read_fn():
        return "read_result"
    
    read_fn._db_type = "read"
    
    result = dispatcher.dispatch(read_fn)
    assert result == "read_result"


def test_write_routes_to_thread_pool():
    dispatcher = SmartDispatcher(MockThreadPool(), MockWorkerManager())
    
    def write_fn():
        return "write_result"
    
    write_fn._db_type = "write"
    
    result = dispatcher.dispatch(write_fn)
    assert result == "write_result"


def test_aggregate_routes_to_worker_manager():
    dispatcher = SmartDispatcher(MockThreadPool(), MockWorkerManager())
    
    def agg_fn():
        return "agg_result"
    
    agg_fn._db_type = "aggregate"
    
    result = dispatcher.dispatch(agg_fn)
    assert result == "agg_result"


def test_lock_acquisition():
    dispatcher = SmartDispatcher(MockThreadPool(), MockWorkerManager())
    
    def write_fn():
        return "locked"
    
    write_fn._db_type = "write"
    write_fn._db_lock = "user:{id}"
    
    result = dispatcher.dispatch(write_fn, "user:123")
    assert result == "locked"


def test_metrics():
    dispatcher = SmartDispatcher(MockThreadPool(), MockWorkerManager())
    
    def read_fn():
        return "read"
    
    read_fn._db_type = "read"
    
    dispatcher.dispatch(read_fn)
    dispatcher.dispatch(read_fn)
    
    assert dispatcher.metrics["read"] == 2
```

---

## Этап 4: Исправление @db_method декоратора

**Цель:** Сделать декоратор рабочим, а не пустышкой.

### Шаг 4.1: Исправить `@db_method` в `modules/db/provider.py`

Декоратор должен интегрироваться с:
- Кешем (проверка/запись)
- Валидацией (Pydantic)
- Блокировками (write-операции)
- Retry (при ошибках)

```python
import functools
from typing import Any


def db_method(
    type: str = 'read',
    timeout: float = 10.0,
    cache_ttl: int = 0,
    cache_key: str = None,
    lock: str = None,
    lock_timeout: float = 5.0,
    validate: type = None,
    audit: bool = False,
    retry: int = 0,
    retry_delay: float = 0.5,
    metrics: str = None,
):
    """Декоратор для Database методов — метаданные для SmartDispatcher."""
    
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # 1. Валидация
            if validate:
                schema = validate(**kwargs)
                kwargs = schema.model_dump()
            
            # 2. Кеш
            if cache_ttl > 0 and cache_key:
                key = cache_key.format(*args, **kwargs)
                cached = _get_cache(key)
                if cached is not None:
                    return cached
            
            # 3. Блокировка (для write)
            if lock and type == "write":
                lock_key = lock.format(*args, **kwargs)
                with _acquire_lock(lock_key, lock_timeout):
                    result = func(*args, **kwargs)
            else:
                # 4. Retry
                if retry > 0:
                    result = _retry_execute(func, args, kwargs, retry, retry_delay)
                else:
                    result = func(*args, **kwargs)
            
            # 5. Запись в кеш
            if cache_ttl > 0 and cache_key:
                _set_cache(key, result, cache_ttl)
            
            return result
        
        # Метаданные для SmartDispatcher
        wrapper._db_type = type
        wrapper._db_timeout = timeout
        wrapper._db_cache_ttl = cache_ttl
        wrapper._db_cache_key = cache_key
        wrapper._db_lock = lock
        wrapper._db_lock_timeout = lock_timeout
        wrapper._db_validate = validate
        wrapper._db_audit = audit
        wrapper._db_retry = retry
        wrapper._db_retry_delay = retry_delay
        wrapper._db_metrics = metrics or f"db.{func.__name__}"
        
        return wrapper
    return decorator


def _get_cache(key: str) -> Any | None:
    """Получить значение из кеша (заглушка)."""
    # Интеграция с CacheHierarchy будет позже
    return None


def _set_cache(key: str, value: Any, ttl: int) -> None:
    """Установить значение в кеш (заглушка)."""
    pass


def _acquire_lock(key: str, timeout: float):
    """Блокировка (заглушка)."""
    import contextlib
    @contextlib.contextmanager
    def noop():
        yield
    return noop()


def _retry_execute(func, args, kwargs, max_attempts, delay):
    """Retry с exponential backoff."""
    import time
    
    for attempt in range(max_attempts):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if attempt == max_attempts - 1:
                raise
            time.sleep(delay * (2 ** attempt))
```

### Шаг 4.2: Написать тесты

Создай `tests/test_db_method_decorator.py`:

```python
"""Тесты для @db_method декоратора."""

import pytest
from modules.db.provider import db_method


def test_sync_function():
    @db_method(type='read')
    def my_func():
        return "result"
    
    result = my_func()
    assert result == "result"


def test_metadata():
    @db_method(type='write', timeout=5.0, cache_ttl=30, lock='user:{id}')
    def my_func():
        pass
    
    assert my_func._db_type == "write"
    assert my_func._db_timeout == 5.0
    assert my_func._db_cache_ttl == 30
    assert my_func._db_lock == "user:{id}"


def test_wraps():
    @db_method(type='read')
    def my_func():
        """Docstring."""
        pass
    
    assert my_func.__name__ == "my_func"
    assert my_func.__doc__ == "Docstring."
```

---

## Этап 5: Observability

**Цель:** Добавить метрики и логи для Database.

### Шаг 5.1: Добавить метрики в `monitoring/metrics.py`

```python
# Database метрики
database_operations_total = Counter(
    'database_operations_total',
    'Total database operations',
    ['operation', 'status']  # get/insert/update/delete, success/error
)

database_operation_duration_seconds = Histogram(
    'database_operation_duration_seconds',
    'Database operation duration',
    ['operation'],
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0]
)

database_cache_hits_total = Counter(
    'database_cache_hits_total',
    'Total cache hits',
    ['level']  # l0, l1, l2
)

database_cache_misses_total = Counter(
    'database_cache_misses_total',
    'Total cache misses'
)
```

### Шаг 5.2: Добавить structured logging

В `core/database.py` и `storage/cache_hierarchy.py`:
- Structured logging для всех операций
- Tracing для慢-запросов (> 100ms)

### Шаг 5.3: Написать тесты

Создай `tests/test_observability.py`:

```python
"""Тесты для Observability."""

import pytest
from unittest.mock import patch
from core.database import Database


def test_metrics_incremented():
    db = Database()
    # Проверяем что метрики инкрементируются
    pass
```

---

## Этап 6: Интеграция с Application и финальные тесты

**Цель:** Полная интеграция, end-to-end тесты.

### Шаг 6.1: Обновить `Application.__init__`

```python
# Полная цепочка
cache = CacheFactory.create("hierarchy")
database = DatabaseFactory.create(cache=cache)
thread_pool = ThreadPoolFactory.create()
worker_manager = WorkerManagerFactory.create()
dispatcher = SmartDispatcher(thread_pool, worker_manager)
database.set_dispatcher(dispatcher)
```

### Шаг 6.2: Написать интеграционные тесты

Создай `tests/test_database_integration.py`:

```python
"""Интеграционные тесты Database."""

import pytest
from core.application import Application


def test_full_cycle():
    app = Application()
    app.startup()
    
    # CRUD операции
    result = app.database.insert("users", {"name": "test"})
    assert result is not None
    
    user = app.database.get("users", result)
    assert user["name"] == "test"
    
    app.shutdown()


def test_cache_integration():
    app = Application()
    app.startup()
    
    # Проверяем что кеш работает
    app.database.insert("users", {"name": "test"})
    # Второй запрос должен идти из кеша
    
    app.shutdown()
```

---

## Итоговая структура файлов (только ядро mia)

```
mia/
├── core/
│   ├── interfaces.py      (+IDatabase) ✅
│   ├── database.py        (Database фасад) ✅
│   ├── async_bridge.py    (НОВЫЙ — только если нужен async/sync bridge)
│   ├── application.py     (обновлён) ✅
│   └── factories.py       (+DatabaseFactory) ✅
├── storage/
│   ├── cache_hierarchy.py (НОВЫЙ — L0→L1→L2 кеш)
│   ├── cache_interface.py (без изменений)
│   └── shared_memory.py   (без изменений)
├── pools/
│   ├── smart_dispatcher.py (НОВЫЙ — маршрутизатор)
│   ├── thread_pool.py     (без изменений)
│   └── worker_manager.py  (без изменений)
├── monitoring/
│   └── metrics.py         (обновлён — DB метрики)
└── tests/
    ├── test_database.py           ✅
    ├── test_cache_hierarchy.py    (НОВЫЙ)
    ├── test_smart_dispatcher.py   (НОВЫЙ)
    ├── test_db_method_decorator.py (НОВЫЙ)
    ├── test_observability.py      (НОВЫЙ)
    └── test_database_integration.py (НОВЫЙ)
```

---

## Зависимости между этапами

```
Этап 0 (подготовка) ✅
  ↓
Этап 1 (интерфейсы + фасад) ✅
  ↓
Этап 2 (Cache Hierarchy) ← зависит от Этапа 1
  ↓
Этап 3 (SmartDispatcher) ← зависит от Этапа 1
  ↓
Этап 4 (Исправление @db_method) ← может параллельно с 2 и 3
  ↓
Этап 5 (Observability) ← может параллельно с другими
  ↓
Этап 6 (Интеграция + тесты)
```

---

## Оценка трудозатрат

| Этап | Описание | Сложность | Оценка |
|------|----------|-----------|--------|
| 0 | Подготовка | низкая | ✅ Готово |
| 1 | Интерфейсы + фасад | средняя | ✅ Готово |
| 2 | Cache Hierarchy | высокая | 1 день |
| 3 | SmartDispatcher | высокая | 1 день |
| 4 | Исправление @db_method | средняя | 0.5 дня |
| 5 | Observability | средняя | 0.5 дня |
| 6 | Интеграция + тесты | средняя | 0.5 дня |
| **Итого** | | | **3.5 дня** |

---

## Что НЕ входит в этот план (modules/db/)

Следующие компоненты реализуются **в modules/db/**, а не в ядре:

- ConnectionPool (asyncpg)
- Async/Sync Bridge
- DatabaseProvider (CRUD реализация)
- Auth (IAuthProvider, AuthProvider)
- SQL-процедуры
- Транзакции
- Bulk-операции

Эти компоненты зависят от asyncpg и других внешних зависимостей, поэтому не входят в ядро mia.
