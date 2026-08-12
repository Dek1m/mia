# План багфиксов: mia — Thread-safety и Data Race

> **Версия:** 1.0  
> **Дата:** 2026-08-12  
> **Автор:** Момо (Planner)  
> **Статус:** Готов к реализации

---

## Введение

Архитектор выявил **2 критических бага** в mia, которые делают систему ненадёжной при многопоточном использовании:

1. **ProcessPool.submit() не thread-safe** — два потока получат чужие результаты из-за отсутствия request ID
2. **SharedMemory без блокировок** — data race при одновременной записи в один сегмент

Оба бага **критичны** для интеграции с belle: если вызовы API идут параллельно (а они идут — `@api_method(parallel=True)`), система будет возвращать неверные результаты и повреждать данные.

**Приоритет: P0 — исправить ДО интеграции с belle.**

---

## Баг #1: ProcessPool.submit() — отсутствие thread-safety

### Описание

Метод `ProcessPool.submit()` (process_pool.py:95-121) кладёт задачу в общую `multiprocessing.Queue` и блочно ждёт результат из общей `_result_queue`. Если два потока вызовут `submit()` одновременно — они оба положат задачу в `_task_queue`, но результат из `_result_queue` заберёт **первый**等待者, а не тот, кто отправил конкретную задачу.

### Причина

Отсутствие request ID. Результаты в `_result_queue` не привязаны к конкретному запросу. Worker кладёт `("ok", result)` без указания, **для какой задачи** этот результат.

### Сценарий бага

```
Поток A: submit(task_1) → кладёт task_1 в queue
Поток B: submit(task_2) → кладёт task_2 в queue
Worker 1: выполняет task_2 → кладёт result_2 в result_queue
Поток A: забирает result_2 из result_queue → ??? Получил чужой результат!
Поток B: забирает result_1 из result_queue → ??? Получил чужой результат!
```

### Решение

1. Каждая задача получает **UUID request_id**
2. Worker возвращает `("ok", request_id, result)` вместо `("ok", result)`
3. Каждый вызывающий поток создаёт свой `multiprocessing.Queue` для получения результата
4. Worker определяет целевую очередь по `request_id`

**Альтернатива (проще):** Использовать `concurrent.futures.ProcessPoolExecutor` из stdlib — он уже решает эту проблему через `Future` объекты. Но это потребует рефакторинга архитектуры.

**Рекомендованное решение:** Модификация текущей архитектуры с request ID + per-request result queues.

### Файлы для изменения

- `/home/opencode/projects/mia/process_pool.py` — основные изменения

### До (текущий код)

```python
def submit(self, fn: Callable, *args, **kwargs) -> Any:
    """Отправить задачу и дождаться результата."""
    if self._task_queue is None:
        raise RuntimeError("ProcessPool not started. Call start() first.")

    self._task_queue.put((fn, args, kwargs))
    self._update_heartbeat_for_active_workers()

    status, result = self._result_queue.get()  # ← БАГ: любой поток заберет любой результат

    self._update_heartbeat_for_active_workers()

    if status == "error":
        raise RuntimeError(f"Task failed: {result}")
    return result
```

### После (исправленный код)

```python
import uuid
import threading
from multiprocessing import Queue
from concurrent.futures import Future

def _worker_entry(task_queue: Queue, result_queue: Queue,
                   worker_id: int, affinity_provider: "CpuAffinityProvider" | None) -> None:
    """Точка входа worker-процесса."""
    signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))

    if affinity_provider:
        core_id = worker_id % affinity_provider.get_cpu_count()
        affinity_provider.set_affinity(0, {core_id})

    log.info("Worker started", extra={"worker_id": worker_id, "pid": os.getpid()})

    while True:
        try:
            task = task_queue.get(timeout=1.0)
            if task is None:
                break
            request_id, fn, args, kwargs = task  # ← Добавлен request_id
            result = fn(*args, **kwargs)
            result_queue.put(("ok", request_id, result))  # ← Возвращаем request_id
        except queue.Empty:
            continue
        except Exception as e:
            log.error("Worker error", extra={"worker_id": worker_id, "error": str(e)})
            result_queue.put(("error", request_id, str(e)))

    log.info("Worker stopped", extra={"worker_id": worker_id})


class ProcessPool:
    def __init__(self, num_processes: int | None = None,
                 affinity_provider: "CpuAffinityProvider" | None = None,
                 heartbeat_monitor: "HeartbeatMonitor" | None = None) -> None:
        self._num_processes = num_processes or os.cpu_count() or 1
        self._affinity = affinity_provider
        self._heartbeat_monitor = heartbeat_monitor
        self._workers: dict[int, multiprocessing.Process] = {}
        self._worker_ids: dict[int, int] = {}
        self._task_queue: multiprocessing.Queue | None = None
        self._result_queue: multiprocessing.Queue | None = None
        self._restart_enabled = heartbeat_monitor is not None
        # ← Добавлено: маппинг request_id → threading.Event для синхронизации
        self._pending_requests: dict[str, threading.Event] = {}
        self._request_results: dict[str, tuple[str, Any]] = {}
        self._pending_lock = threading.Lock()
        log.info("ProcessPool created", extra={"num_processes": self._num_processes})

    def submit(self, fn: Callable, *args, **kwargs) -> Any:
        """Отправить задачу и дождаться результата (thread-safe)."""
        if self._task_queue is None:
            raise RuntimeError("ProcessPool not started. Call start() first.")

        request_id = str(uuid.uuid4())

        # Создать Event для ожидания результата
        event = threading.Event()
        with self._pending_lock:
            self._pending_requests[request_id] = event

        self._task_queue.put((request_id, fn, args, kwargs))
        self._update_heartbeat_for_active_workers()

        # Ждать результат именно для этого request_id
        event.wait()

        with self._pending_lock:
            status, result = self._request_results.pop(request_id)
            del self._pending_requests[request_id]

        self._update_heartbeat_for_active_workers()

        if status == "error":
            raise RuntimeError(f"Task failed: {result}")
        return result

    def _result_listener(self) -> None:
        """Фоновый поток: читает результаты и маршрутизирует по request_id."""
        while self._result_queue is not None:
            try:
                status, request_id, result = self._result_queue.get(timeout=1.0)
                with self._pending_lock:
                    if request_id in self._pending_requests:
                        self._request_results[request_id] = (status, result)
                        self._pending_requests[request_id].set()
            except queue.Empty:
                continue
            except Exception as e:
                log.error("Result listener error", extra={"error": str(e)})
```

### Сложность

**Высокая** — затрагивает архитектуру взаимодействия процессов, требует нового фонового потока-слушателя.

### Зависимости

- Требуется изменение протокола между main-процессом и workers
- Workers нужно перезапустить (обратно несовместимое изменение формата очереди)

---

## Баг #2: SharedMemory — отсутствие блокировок

### Описание

`SharedMemoryManager` (shared_memory.py) не имеет никаких механизмов синхронизации. Методы `create()`, `attach()`, `cleanup()` и любой доступ к сегментам не защищены от concurrent access.

### Причина

Отсутствие `threading.Lock` или `multiprocessing.Lock` для защиты операций с разделяемой памятью.

### Сценарий бага

```
Поток A: segment.buf[:4] = b'\x01\x02\x03\x04'  # пишет данные
Поток B: segment.buf[:4] = b'\xff\xfe\xfd\xfc'  # перезаписывает
Поток A: читает buf[:4] → получил b'\xff\xfe\xfd\xfc' вместо своих данных
```

### Решение

1. Добавить `threading.Lock` для каждого сегмента
2. Предоставить контекстный менеджер `lock()` для безопасного доступа
3. Добавить `get_locked()` метод для получения сегмента с автоматической блокировкой

### Файлы для изменения

- `/home/opencode/projects/mia/shared_memory.py` — основные изменения

### До (текущий код)

```python
class SharedMemoryManager:
    """Менеджер разделяемой памяти."""
    
    def __init__(self) -> None:
        self._segments: dict[str, shm.SharedMemory] = {}
    
    def create(self, name: str, size: int) -> shm.SharedMemory:
        """Создать сегмент разделяемой памяти."""
        segment = shm.SharedMemory(create=True, size=size, name=name)
        self._segments[name] = segment
        return segment
    
    def attach(self, name: str) -> shm.SharedMemory:
        """Подключиться к существующему сегменту."""
        segment = shm.SharedMemory(name=name)
        self._segments[name] = segment
        return segment
    
    def cleanup(self) -> None:
        """Очистить все сегменты."""
        for name, segment in self._segments.items():
            try:
                segment.close()
                segment.unlink()
            except Exception as e:
                log.error("Failed to cleanup SharedMemory", extra={"name": name, "error": str(e)})
        self._segments.clear()
```

### После (исправленный код)

```python
import threading
import multiprocessing.shared_memory as shm
from contextlib import contextmanager
from typing import Any, Generator


class LockedSharedMemory:
    """Обёртка над SharedMemory с блокировкой."""

    def __init__(self, segment: shm.SharedMemory, lock: threading.Lock) -> None:
        self._segment = segment
        self._lock = lock

    @property
    def buf(self) -> memoryview:
        """Буфер сегмента. Используй через context manager!"""
        return self._segment.buf

    @property
    def name(self) -> str:
        return self._segment.name

    @property
    def size(self) -> int:
        return self._segment.size

    @contextmanager
    def lock(self) -> Generator[None, None, None]:
        """Контекстный менеджер для безопасного доступа.

        Usage:
            with locked_segment.lock():
                locked_segment.buf[:4] = data
        """
        self._lock.acquire()
        try:
            yield
        finally:
            self._lock.release()

    def read(self, offset: int = 0, size: int | None = None) -> bytes:
        """Потокобезопасное чтение данных."""
        with self._lock:
            end = offset + size if size else len(self._segment.buf)
            return bytes(self._segment.buf[offset:end])

    def write(self, data: bytes, offset: int = 0) -> None:
        """Потокобезопасная запись данных."""
        with self._lock:
            self._segment.buf[offset:offset + len(data)] = data

    def close(self) -> None:
        self._segment.close()

    def unlink(self) -> None:
        self._segment.unlink()


class SharedMemoryManager:
    """Менеджер разделяемой памяти с поддержкой thread-safety."""
    
    def __init__(self) -> None:
        self._segments: dict[str, LockedSharedMemory] = {}
        self._manager_lock = threading.Lock()
    
    def create(self, name: str, size: int) -> LockedSharedMemory:
        """Создать сегмент разделяемой памяти."""
        with self._manager_lock:
            if name in self._segments:
                raise ValueError(f"Segment '{name}' already exists")
            segment = shm.SharedMemory(create=True, size=size, name=name)
            lock = threading.Lock()
            locked = LockedSharedMemory(segment, lock)
            self._segments[name] = locked
            log.info("SharedMemory segment created", extra={"name": name, "size": size})
            return locked
    
    def attach(self, name: str) -> LockedSharedMemory:
        """Подключиться к существующему сегменту."""
        with self._manager_lock:
            if name in self._segments:
                return self._segments[name]
            segment = shm.SharedMemory(name=name)
            lock = threading.Lock()
            locked = LockedSharedMemory(segment, lock)
            self._segments[name] = locked
            log.info("SharedMemory segment attached", extra={"name": name})
            return locked
    
    def get(self, name: str) -> LockedSharedMemory | None:
        """Получить сегмент по имени."""
        return self._segments.get(name)
    
    def cleanup(self) -> None:
        """Очистить все сегменты."""
        with self._manager_lock:
            for name, locked in self._segments.items():
                try:
                    locked.close()
                    locked.unlink()
                except Exception as e:
                    log.error("Failed to cleanup SharedMemory", extra={"name": name, "error": str(e)})
            self._segments.clear()
            log.info("SharedMemory cleaned up")
```

### Сложность

**Средняя** — изолированные изменения, не затрагивают другие модули.

### Зависимости

- Нет (можно делать параллельно с Багом #1)

---

## Приоритизация

| # | Баг | Приоритет | Сложность | Время | Порядок |
|---|-----|-----------|-----------|-------|---------|
| 1 | ProcessPool thread-safety | **P0** | Высокая | 4-6ч | **Первым** |
| 2 | SharedMemory data race | **P0** | Средняя | 2-3ч | Вторым |

**Почему ProcessPool первым:**
- Это **критический путь** — все API-вызовы с `parallel=True` проходят через него
- Без этого фикса система **возвращает неверные результаты**
- SharedMemory пока используется ограниченно, а ProcessPool — каждый день

---

## Тесты

### Тесты для Бага #1 (ProcessPool)

| Тест | Что проверяет | Файл |
|------|---------------|------|
| `test_concurrent_submit_thread_safe` | 10 потоков отправляют задачи одновременно, каждый получает свой результат | `tests/test_process_pool.py` |
| `test_request_id_isolation` | Два разных request_id не пересекаются | `tests/test_process_pool.py` |
| `test_submit_after_shutdown_raises` | submit после shutdown бросает RuntimeError | `tests/test_process_pool.py` |
| `test_worker_restart_preserves_queue` | После перезапуска worker'а очередь задач не теряется | `tests/test_process_pool.py` |

### Тесты для Бага #2 (SharedMemory)

| Тест | Что проверяет | Файл |
|------|---------------|------|
| `test_concurrent_write_no_corruption` | 10 потоков пишут в один сегмент — данные не смешиваются | `tests/test_shared_memory.py` |
| `test_lock_context_manager` | `with segment.lock()` блокирует другие потоки | `tests/test_shared_memory.py` |
| `test_read_write_atomicity` | `read()` и `write()` атомарны | `tests/test_shared_memory.py` |
| `test_cleanup_thread_safe` | `cleanup()` во время записи не крашит | `tests/test_shared_memory.py` |

---

## Оценка времени

| Задача | Время | Ответственный |
|--------|-------|---------------|
| Рефакторинг ProcessPool (request ID + listener) | 4-6ч | **Сона** |
| Рефакторинг SharedMemory (locks + LockedSharedMemory) | 2-3ч | **Сона** |
| Тесты для ProcessPool | 2-3ч | **Катерина** |
| Тесты для SharedMemory | 1-2ч | **Катерина** |
| Code review | 1ч | **Эна** |
| **Итого** | **10-15ч (1.5-2 дня)** | |

---

## Риски

| Риск | Вероятность | Влияние | Митигация |
|------|-------------|---------|-----------|
| Worker-процесс не поддерживает threading.Event (fork context) | средняя | высокое | Использовать `multiprocessing.Event` вместо `threading.Event` в main-процессе |
| Deadlock в result_listener при падении worker'а | низкая | среднее | Таймаут в listener,定期检查 worker health |
| SharedMemory lock не защищает от cross-process racing | средняя | среднее | Для межпроцессного взаимодействия использовать `multiprocessing.Lock` вместо `threading.Lock` |
| Обратно несовместимый API (breaking change) | гарантированно | низкое | Версионирование (minor bump), миграция простая |

---

## Дополнительные замечания

### Кросс-процессные блокировки

Текущее решение использует `threading.Lock` — это защищает от **многопоточного** доступа внутри одного процесса. Для **межпроцессного** доступа нужен `multiprocessing.Lock`.

**Вопрос к архитектору:** SharedMemory в mia используется только внутри одного процесса (между потоками) или между процессами?

- Если **между потоками** → `threading.Lock` достаточно
- Если **между процессами** → нужен `multiprocessing.Lock` или `multiprocessing.Manager().Lock()`

### Альтернатива ProcessPool

Самое чистое решение — заменить кастомный `ProcessPool` на `concurrent.futures.ProcessPoolExecutor` из stdlib. Он уже:
- Thread-safe
- Использует `Future` объекты
- Поддерживает cancellation
- Правильно обрабатывает ошибки

**Но:** это потребует рефакторинга архитектуры (push → pull модель, worker entry point и т.д.). Рекомендую это как **отдельную задачу** после исправления текущих багов.

---

*План готов. Ожидает утверждения Team Lead (Афина) и старта реализации.*
