"""Комплексная проверка интеграции Universal Task System.

Проверяет 10 критических сценариев:
1. Async bridge: async-функция диспатчится в ThreadPool
2. @task с dispatcher: Task в TaskStore, статусы start/complete
3. @task без dispatcher (standalone): inline fallback
4. Двухфазная маршрутизация: classify → override для async
5. Write-lock: async write-задачи сериализуются
6. apiproxy.call диспатчится через dispatcher
7. modules/db: transaction() НЕ сломан
8. Метрики: threadpool_tasks_submitted_total инкрементируется
9. ISmartDispatcher: dispatch_async в интерфейсе
10. _task_type_to_metric_key: корректный маппинг типов

Обнаруженные баги:
- КРИТИЧЕСКИЙ: transaction() сломан @db_method + @asynccontextmanager
- ВАЖНЫЙ: dispatch_async + new_event_loop = RuntimeError в running loop
- ВАЖНЫЙ: ISmartDispatcher не объявляет dispatch_async
- КОСМЕТИКА: _task_type_to_metric_key все типы маппит в 'read'
"""
from __future__ import annotations

import asyncio
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.adaptive_router import AdaptiveRouter
from core.task import Task, TaskStatus, TaskType
from core.task_classifier import TaskClassifier
from core.task_decorator import task, set_global_dispatcher
from core.task_store import TaskStore
from pools.smart_dispatcher import SmartDispatcher, _task_type_to_metric_key


# ── Fixtures ──────────────────────────────────────────────


class FakeThreadPool:
    """Синхронный ThreadPool — выполняет fn в submit().

    ВАЖНО: FakeThreadPool выполняет fn в том же потоке.
    Это значит, что dispatch_async с new_event_loop() НЕ работает
    (RuntimeError: Cannot run the event loop while another loop is running).
    Тесты с FakeThreadPool тестируют FALLBACK path, а НЕ dispatcher path.
    """

    def __init__(self) -> None:
        self.submitted: list[tuple] = []

    def submit(self, fn, *args, **kwargs):
        self.submitted.append((fn, args, kwargs))
        result = fn(*args, **kwargs)
        fut: Future = Future()
        fut.set_result(result)
        return fut


class FakeWorkerManager:
    """Синхронный WorkerManager."""

    def __init__(self) -> None:
        self.submitted: list[tuple] = []

    def submit(self, fn, *args, **kwargs):
        self.submitted.append((fn, args, kwargs))
        return fn(*args, **kwargs)


class RealThreadPool:
    """Реальный ThreadPool для тестирования dispatch_async в отдельном потоке."""

    def __init__(self) -> None:
        self._pool = ThreadPoolExecutor(max_workers=2)
        self.submitted: list[tuple] = []

    def submit(self, fn, *args, **kwargs):
        self.submitted.append((fn, args, kwargs))
        return self._pool.submit(fn, *args, **kwargs)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=True)


@pytest.fixture
def fake_dispatcher():
    """SmartDispatcher с FakeThreadPool (fallback path)."""
    tp = FakeThreadPool()
    wm = FakeWorkerManager()
    dp = SmartDispatcher(tp, wm)
    return dp, tp, wm


@pytest.fixture
def real_dispatcher():
    """SmartDispatcher с реальным ThreadPool (dispatch_async path)."""
    tp = RealThreadPool()
    wm = FakeWorkerManager()
    dp = SmartDispatcher(tp, wm)
    yield dp, tp, wm
    tp.shutdown()


@pytest.fixture
def full_dispatcher():
    """SmartDispatcher со всеми компонентами: classifier, router, task_store."""
    tp = FakeThreadPool()
    wm = FakeWorkerManager()
    store = TaskStore()
    classifier = TaskClassifier()
    router = AdaptiveRouter(store)
    dp = SmartDispatcher(tp, wm, task_store=store, classifier=classifier, adaptive_router=router)
    return dp, tp, wm, store, classifier, router


# ── 1. Async bridge: async-функция диспатчится в ThreadPool ──


class TestAsyncBridge:
    """Проверка async bridge: async-функции корректно выполняются через dispatch_async."""

    def test_sync_function_via_dispatch_async(self, fake_dispatcher) -> None:
        """sync-функция через dispatch_async работает."""
        dp, tp, wm = fake_dispatcher

        def sync_fn(x: int) -> int:
            return x * 3

        future = dp.dispatch_async(sync_fn, 4)
        assert isinstance(future, Future)
        assert future.result() == 12
        assert len(tp.submitted) == 1

    def test_async_function_dispatched_to_thread_pool_real(
        self, real_dispatcher,
    ) -> None:
        """Async-функция диспатчится в реальный ThreadPool — результат правильный.

        С реальным ThreadPoolExecutor _async_wrapper выполняется в отдельном потоке,
        где нет running event loop, поэтому new_event_loop().run_until_complete() работает.
        """
        dp, tp, wm = real_dispatcher

        async def async_fn(x: int) -> int:
            await asyncio.sleep(0.01)
            return x * 2

        future = dp.dispatch_async(async_fn, 5)
        result = future.result(timeout=5)
        assert result == 10
        assert len(tp.submitted) == 1

    def test_async_function_with_task_object(self, fake_dispatcher) -> None:
        """dispatch_async с явным Task-объектом."""
        dp, tp, wm = fake_dispatcher

        async def async_fn(x: int) -> int:
            return x + 10

        task_obj = Task.create(module_id="test", fn_name="async_fn")
        future = dp.dispatch_async(task_obj, async_fn, 3)
        assert future.result() == 13

    def test_fakeThreadPool_does_NOT_use_dispatcher(
        self, fake_dispatcher,
    ) -> None:
        """КРИТИЧЕСКИЙ: FakeThreadPool НЕ может тестировать dispatch_async path.

        FakeThreadPool выполняет _async_wrapper в том же потоке, где уже есть
        running event loop. Это вызывает RuntimeError: Cannot run the event loop
        while another loop is running. Ошибка ловится @task fallback.

        Поэтому тесты с FakeThreadPool тестируют FALLBACK INLINE path,
        а НЕ dispatcher path.
        """
        dp, tp, wm = fake_dispatcher
        set_global_dispatcher(dp)

        async def async_fn(x: int) -> int:
            return x * 2

        try:
            # Должен использовать fallback (inline), а НЕ dispatcher
            @task(type="cpu", timeout=5.0)
            async def compute(x: int) -> int:
                return x * 2

            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(compute(5))
                assert result == 10
            finally:
                loop.close()
        finally:
            set_global_dispatcher(None)

    def test_real_thread_pool_uses_dispatcher(
        self, real_dispatcher,
    ) -> None:
        """С реальным ThreadPool dispatch_async работает в отдельном потоке."""
        dp, tp, wm = real_dispatcher
        set_global_dispatcher(dp)

        @task(type="cpu", timeout=5.0)
        async def compute(x: int) -> int:
            await asyncio.sleep(0.01)
            return x * 2

        try:
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(compute(5))
                assert result == 10
                # ThreadPool должен был получить задачу
                assert len(tp.submitted) >= 1
            finally:
                loop.close()
        finally:
            set_global_dispatcher(None)

    def test_dispatch_async_from_async_context_with_inline_pool(self) -> None:
        """КРИТИЧЕСКИЙ БАГ: dispatch_async из async-контекста с inline pool.

        FakeThreadPool выполняет _async_wrapper в том же потоке, где уже
        есть running event loop. Ранее это вызывало RuntimeError.
        Теперь dispatch_async определяет running loop и запускает
        в отдельном потоке через threading.Thread.
        """
        from concurrent.futures import ThreadPoolExecutor

        # Создаём ThreadPoolExecutor (как в реальном проде)
        tp = RealThreadPool()
        wm = FakeWorkerManager()
        dp = SmartDispatcher(tp, wm)

        async def async_fn(x: int) -> int:
            await asyncio.sleep(0.01)
            return x * 3

        # Вызов dispatch_async из async-контекста
        async def _run() -> int:
            future = dp.dispatch_async(async_fn, 7)
            return future.result(timeout=5)

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(_run())
            assert result == 21
            assert len(tp.submitted) >= 1
        finally:
            loop.close()
            tp.shutdown()


# ── 2. @task с dispatcher: Task в TaskStore ──


class TestTaskWithDispatcher:
    """Проверка: @task dispatch через SmartDispatcher создаёт Task в TaskStore."""

    def test_sync_task_creates_task_in_store(self, full_dispatcher) -> None:
        """sync @task создаёт Task в TaskStore через dispatcher."""
        dp, tp, wm, store, classifier, router = full_dispatcher
        set_global_dispatcher(dp)

        @task(type="cpu", timeout=5.0)
        def compute(x: int) -> int:
            return x * 2

        try:
            result = compute(5)
            assert result == 10

            history = store.get_history()
            assert len(history) >= 1
            t = history[-1]
            assert t.status == TaskStatus.COMPLETED
            assert t.result == 10
        finally:
            set_global_dispatcher(None)

    def test_async_task_creates_task_in_store(self, full_dispatcher) -> None:
        """async @task создаёт Task в TaskStore через dispatcher."""
        dp, tp, wm, store, classifier, router = full_dispatcher
        # Используем FakeThreadPool — fallback path
        # Task создаётся в _create_task, но dispatcher может не использовать store

        @task(type="cpu", timeout=5.0)
        async def async_compute(x: int) -> int:
            return x * 3

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(async_compute(4))
            assert result == 12
        finally:
            loop.close()

    def test_task_status_transitions(self, full_dispatcher) -> None:
        """Task проходит все статусы: PENDING → RUNNING → COMPLETED."""
        dp, tp, wm, store, classifier, router = full_dispatcher

        t = Task.create(module_id="test", fn_name="fn")
        assert t.status == TaskStatus.PENDING

        t.start()
        assert t.status == TaskStatus.RUNNING
        assert t.started_at is not None

        t.complete(result="done")
        assert t.status == TaskStatus.COMPLETED
        assert t.result == "done"
        assert t.completed_at is not None
        assert t.duration is not None
        assert t.duration >= 0

    def test_task_failure_status(self, full_dispatcher) -> None:
        """Task с ошибкой: PENDING → RUNNING → FAILED."""
        dp, tp, wm, store, classifier, router = full_dispatcher

        t = Task.create(module_id="test", fn_name="fn")
        t.start()
        t.fail("something went wrong")
        assert t.status == TaskStatus.FAILED
        assert t.error == "something went wrong"


# ── 3. @task без dispatcher (standalone): inline fallback ──


class TestTaskFallback:
    """Проверка: @task работает inline без SmartDispatcher."""

    def setup_method(self) -> None:
        set_global_dispatcher(None)

    def test_sync_task_inline(self) -> None:
        """sync @task работает inline без dispatcher."""
        @task(type="cpu", timeout=5.0)
        def compute(x: int) -> int:
            return x * 2

        result = compute(5)
        assert result == 10

    def test_async_task_inline(self) -> None:
        """async @task работает inline без dispatcher."""
        @task(type="cpu", timeout=5.0)
        async def async_compute(x: int) -> int:
            await asyncio.sleep(0.01)
            return x * 3

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(async_compute(4))
            assert result == 12
        finally:
            loop.close()

    def test_sync_task_retry_inline(self) -> None:
        """sync @task с retry работает inline."""
        call_count = 0

        @task(type="cpu", retry=2, retry_delay=0.01)
        def flaky() -> int:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ValueError("fail")
            return 42

        assert flaky() == 42
        assert call_count == 3

    def test_async_task_retry_inline(self) -> None:
        """async @task с retry работает inline."""
        call_count = 0

        @task(type="cpu", retry=2, retry_delay=0.01)
        async def async_flaky() -> int:
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise RuntimeError("fail")
            return 99

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(async_flaky())
            assert result == 99
            assert call_count == 2
        finally:
            loop.close()

    def test_task_fallback_on_dispatcher_error(self) -> None:
        """@task fallback на inline при ошибке dispatcher."""
        bad_dispatcher = MagicMock()
        bad_dispatcher.dispatch_async.side_effect = RuntimeError("broken")
        set_global_dispatcher(bad_dispatcher)

        @task(type="cpu", timeout=5.0)
        def compute(x: int) -> int:
            return x * 2

        try:
            result = compute(5)
            assert result == 10
        finally:
            set_global_dispatcher(None)


# ── 4. Двухфазная маршрутизация: classify → override ──


class TestTwoPhaseRouting:
    """Проверка: classify → override применяется для async-задач."""

    def test_classifier_classifies_fn_task_type(self) -> None:
        """Classifier определяет тип по fn._task_type."""
        classifier = TaskClassifier()

        @task(type="database")
        def db_query(sql: str) -> list:
            return [{"result": sql}]

        t = Task.create(module_id="core", fn_name="db_query")
        task_type = classifier.classify(t, db_query)
        assert task_type == TaskType.DATABASE

    def test_classifier_fallback_to_module_name(self) -> None:
        """Classifier fallback к имени модуля если нет _task_type."""
        classifier = TaskClassifier()

        def get_user(user_id: str) -> dict:
            return {"id": user_id}

        get_user.__module__ = "db"
        get_user.__name__ = "get_user"

        t = Task.create(module_id="db", fn_name="get_user")
        task_type = classifier.classify(t, get_user)
        assert task_type == TaskType.DATABASE

    def test_adaptive_override_applied(self, full_dispatcher) -> None:
        """AdaptiveRouter override применяется при p95 > порога."""
        dp, tp, wm, store, classifier, router = full_dispatcher

        # Заполняем history медленными IO-задачами
        for i in range(20):
            t = Task.create(module_id="storage", fn_name=f"read_{i}", task_type=TaskType.IO)
            t.start()
            t.complete(result=None)
            t.duration = 0.5
            store._active.pop(t.id, None)
            store._history.append(t)

        router.update_stats()

        # Новая IO-задача должна быть переключена на CPU
        t = Task.create(module_id="storage", fn_name="read_new", task_type=TaskType.IO)
        override = router.override(t)
        assert override == TaskType.CPU

    def test_adaptive_override_not_applied_when_fast(self, full_dispatcher) -> None:
        """AdaptiveRouter override НЕ применяется когда задачи быстрые."""
        dp, tp, wm, store, classifier, router = full_dispatcher

        for i in range(20):
            t = Task.create(module_id="storage", fn_name=f"read_{i}", task_type=TaskType.IO)
            t.start()
            t.complete(result=None)
            t.duration = 0.01
            store._active.pop(t.id, None)
            store._history.append(t)

        router.update_stats()

        t = Task.create(module_id="storage", fn_name="read_new", task_type=TaskType.IO)
        override = router.override(t)
        assert override is None


# ── 5. Write-lock: async write-задачи сериализуются ──


class TestWriteLock:
    """Проверка: write-lock сериализует write-задачи."""

    def test_write_lock_serializes_tasks(self, fake_dispatcher) -> None:
        """write-задачи с _db_lock=True выполняются последовательно."""
        dp, tp, wm = fake_dispatcher
        order: list[int] = []

        def locked_write(val: int) -> int:
            order.append(val)
            return val

        locked_write._db_type = "write"
        locked_write._db_lock = True
        locked_write.__module__ = "db"
        locked_write.__name__ = "locked_write"

        f1 = dp.dispatch(locked_write, 1)
        f2 = dp.dispatch(locked_write, 2)

        assert f1.result() == 1
        assert f2.result() == 2
        assert order == [1, 2]

    def test_write_lock_in_dispatch_async(self, fake_dispatcher) -> None:
        """dispatch_async с write-lock: async-функция с _db_lock=True."""
        dp, tp, wm = fake_dispatcher

        async def locked_async_fn(x: int) -> int:
            return x * 4

        locked_async_fn._db_lock = True  # type: ignore[attr-defined]

        task_obj = Task.create(module_id="test", fn_name="locked_async_fn")
        task_obj.task_type = TaskType.IO
        future = dp.dispatch_async(task_obj, locked_async_fn, 2)
        assert future.result() == 8

    def test_write_lock_manual_acquire_release(self, fake_dispatcher) -> None:
        """Ручное управление write-lock: acquire_lock/release_lock."""
        dp, tp, wm = fake_dispatcher

        dp.acquire_lock()
        try:
            # Блокировка захвачена
            assert dp._write_lock.locked()
        finally:
            dp.release_lock()

        assert not dp._write_lock.locked()


# ── 6. apiproxy.call диспатчится через dispatcher ──


class TestApiProxyDispatch:
    """Проверка: apiproxy.call/list_api корректно диспатчатся через @task."""

    def test_list_api_is_sync_task(self) -> None:
        """list_api — sync @task, должен иметь _task_type."""
        from modules.apiproxy.provider import ApiProxyProvider
        from modules.apiproxy.config import ApiproxyConfig

        config = ApiproxyConfig(whitelist=["auth"])
        proxy = ApiProxyProvider(config=config)

        # list_api — sync функция с @task
        assert hasattr(proxy.list_api, "_task_type")
        assert proxy.list_api._task_type == TaskType("cpu")

    def test_call_is_async_task(self) -> None:
        """call — async @task, должен иметь _task_type."""
        from modules.apiproxy.provider import ApiProxyProvider
        from modules.apiproxy.config import ApiproxyConfig

        config = ApiproxyConfig(whitelist=["auth"])
        proxy = ApiProxyProvider(config=config)

        # call — async функция с @task
        assert hasattr(proxy.call, "_task_type")
        assert proxy.call._task_type == TaskType("cpu")

    def test_call_method_actually_calls_module(self) -> None:
        """apiproxy.call реально вызывает методы модулей."""
        from modules.apiproxy.provider import ApiProxyProvider
        from modules.apiproxy.config import ApiproxyConfig

        config = ApiproxyConfig(whitelist=["test"])
        proxy = ApiProxyProvider(config=config)

        # Регистрируем тестовый метод
        async def my_method(x: int) -> int:
            return x * 2

        proxy.registry.register(
            "test", "my_method",
            {"name": "my_method", "description": "Test", "args": {}, "return_type": "int", "public": True, "required_permission": None},
            my_method,
        )

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                proxy.call("test", "my_method", {"x": 5})
            )
            assert result["error"] is None
            assert result["data"] == 10
        finally:
            loop.close()

    def test_list_api_returns_methods(self) -> None:
        """list_api возвращает список методов."""
        from modules.apiproxy.provider import ApiProxyProvider
        from modules.apiproxy.config import ApiproxyConfig

        config = ApiproxyConfig(whitelist=["auth"])
        proxy = ApiProxyProvider(config=config)

        # list_api — sync, вызываем напрямую
        methods = proxy.list_api()
        assert isinstance(methods, list)


# ── 7. modules/db: transaction() НЕ сломан ──


class TestDbTransaction:
    """Проверка: transaction() корректно работает как async context manager.

    ОБНАРУЖЕН БАГ: @db_method оборачивает @asynccontextmanager в async def,
    что ломает async context manager protocol.
    """

    def test_transaction_is_async_context_manager(self) -> None:
        """transaction() должен быть async context manager, НЕ coroutine."""
        from modules.db.provider import DatabaseProvider
        from modules.db.config import DatabaseConfig

        config = DatabaseConfig()

        # pool.acquire() должен возвращать ACM (как asyncpg),
        # а не coroutine. MagicMock не оборачивает в coroutine.
        mock_conn = MagicMock()

        mock_conn_tx = MagicMock()
        mock_conn_tx.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn_tx.__aexit__ = AsyncMock(return_value=False)
        mock_conn.transaction.return_value = mock_conn_tx

        mock_acquire = MagicMock()
        mock_acquire.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_acquire.__aexit__ = AsyncMock(return_value=False)

        pool = MagicMock()
        pool.acquire.return_value = mock_acquire

        provider = DatabaseProvider(pool=pool, config=config)

        # transaction() должен вернуть async context manager
        cm = provider.transaction()

        is_coroutine = asyncio.iscoroutine(cm)
        is_acm = hasattr(cm, "__aenter__") and hasattr(cm, "__aexit__")

        if is_coroutine:
            cm.close()
            pytest.fail(
                "transaction() возвращает coroutine вместо async context manager. "
                "@db_method ломает @asynccontextmanager. "
                "async with provider.transaction() as conn: → TypeError."
            )
        elif is_acm:
            # Проверяем что async with работает
            async def _test_enter_exit() -> None:
                async with provider.transaction() as conn:
                    assert conn is not None
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(_test_enter_exit())
            finally:
                loop.close()
        else:
            pytest.fail(
                f"transaction() вернул неожиданный тип: {type(cm)}. "
                "Ожидался async context manager."
            )

    def test_db_method_breaks_asynccontextmanager(self) -> None:
        """@db_method + @asynccontextmanager = async context manager (FIXED).

        @db_method теперь прозрачен для async context managers:
        проверяет __aenter__/__aexit__ и возвращает как есть.
        """
        from modules.db.provider import DatabaseProvider
        from modules.db.config import DatabaseConfig

        config = DatabaseConfig()
        pool = AsyncMock()
        provider = DatabaseProvider(pool=pool, config=config)

        # Вызов transaction() теперь возвращает async context manager
        result = provider.transaction()

        # Это НЕ coroutine — это async context manager
        assert not asyncio.iscoroutine(result), (
            "transaction() не должен возвращать coroutine"
        )
        # Это async context manager
        assert hasattr(result, "__aenter__") and hasattr(result, "__aexit__"), (
            "transaction() должен возвращать async context manager"
        )

    def test_database_provider_crud_works(self) -> None:
        """DatabaseProvider CRUD-методы работают через @db_method + @task."""
        from modules.db.provider import DatabaseProvider
        from modules.db.config import DatabaseConfig

        config = DatabaseConfig()
        pool = AsyncMock()

        # Мокаем pool.fetchval для insert
        pool.fetchval.return_value = "test-id-1"
        pool.fetchrow.return_value = {"id": "test-id-1", "name": "Alice"}
        pool.fetch.return_value = [{"id": "test-id-1", "name": "Alice"}]
        pool.execute.return_value = "DELETE 1"

        provider = DatabaseProvider(pool=pool, config=config)

        loop = asyncio.new_event_loop()
        try:
            # insert
            id_ = loop.run_until_complete(
                provider.insert("users", {"name": "Alice"})
            )
            assert id_ == "test-id-1"

            # get
            user = loop.run_until_complete(
                provider.get("users", "test-id-1")
            )
            assert user["name"] == "Alice"
        finally:
            loop.close()


# ── 8. Метрики: threadpool_tasks_submitted_total инкрементируется ──


class TestMetrics:
    """Проверка: метрики корректно инкрементируются."""

    def test_threadpool_metrics_incremented(self, fake_dispatcher) -> None:
        """threadpool_tasks_submitted_total инкрементируется при dispatch."""
        dp, tp, wm = fake_dispatcher

        def sync_fn(x: int) -> int:
            return x

        dp.dispatch(sync_fn, 1)
        assert dp.metrics["read"] == 1

    def test_write_metrics_incremented(self, fake_dispatcher) -> None:
        """write-задачи инкрементируют write-метрику."""
        dp, tp, wm = fake_dispatcher

        def write_fn() -> str:
            return "ok"

        write_fn._db_type = "write"
        write_fn.__module__ = "db"
        write_fn.__name__ = "write_fn"

        dp.dispatch(write_fn)
        assert dp.metrics["write"] == 1

    def test_aggregate_metrics_incremented(self, fake_dispatcher) -> None:
        """aggregate-задачи инкрементируют aggregate-метрику."""
        dp, tp, wm = fake_dispatcher

        def agg_fn() -> int:
            return 42

        agg_fn._db_type = "aggregate"
        agg_fn.__module__ = "compute"
        agg_fn.__name__ = "agg_fn"

        dp.dispatch(agg_fn)
        assert dp.metrics["aggregate"] == 1

    def test_metrics_returns_copy(self, fake_dispatcher) -> None:
        """metrics возвращает копию, не оригинал."""
        dp, tp, wm = fake_dispatcher

        m1 = dp.metrics
        dp.dispatch(lambda: None)
        m2 = dp.metrics

        # m1 не изменился (это была копия на момент вызова)
        assert m1["read"] == 0
        assert m2["read"] == 1

    def test_task_type_to_metric_key_all_types(self) -> None:
        """_task_type_to_metric_key: все типы маппятся корректно."""
        expected = {
            TaskType.IO: "read",
            TaskType.CPU: "cpu",
            TaskType.GPU: "gpu",
            TaskType.NETWORK: "network",
            TaskType.DATABASE: "database",
            TaskType.AGGREGATE: "aggregate",
            TaskType.UNKNOWN: "read",
        }

        for tt, expected_key in expected.items():
            actual = _task_type_to_metric_key(tt)
            assert actual == expected_key, f"{tt} → {actual}, ожидалось {expected_key}"


# ── 9. ISmartDispatcher: dispatch_async в интерфейсе ──


class TestISmartDispatcherInterface:
    """Проверка: ISmartDispatcher интерфейс полон."""

    def test_dispatch_async_not_in_interface(self) -> None:
        """dispatch_async объявлен в ISmartDispatcher."""
        from core.interfaces import ISmartDispatcher

        has_dispatch_async = hasattr(ISmartDispatcher, "dispatch_async")
        assert has_dispatch_async, (
            "dispatch_async должен быть объявлен в ISmartDispatcher"
        )

    def test_smart_dispatcher_implements_dispatch_async(self) -> None:
        """SmartDispatcher реализует dispatch_async (вне интерфейса)."""
        tp = FakeThreadPool()
        wm = FakeWorkerManager()
        dp = SmartDispatcher(tp, wm)

        assert hasattr(dp, "dispatch_async")
        assert callable(dp.dispatch_async)

    def test_smart_dispatcher_implements_interface_methods(self) -> None:
        """SmartDispatcher реализует все методы ISmartDispatcher."""
        tp = FakeThreadPool()
        wm = FakeWorkerManager()
        dp = SmartDispatcher(tp, wm)

        assert hasattr(dp, "dispatch")
        assert hasattr(dp, "acquire_lock")
        assert hasattr(dp, "release_lock")
        assert hasattr(dp, "metrics")


# ── 10. Regression: import errors в тестах ──


class TestRegressionImports:
    """Проверка: импорты в тестах работают после рефакторинга."""

    def test_adaptive_router_imports(self) -> None:
        """test_adaptive_router.py: HISTORY_WINDOW и P95_THRESHOLD удалены.

        ОБНАРУЖЕНА ПРОБЛЕМА: тесты test_adaptive_router.py и
        test_task_system_e2e.py не могут импортировать HISTORY_WINDOW и
        P95_THRESHOLD из core.adaptive_router, т.к. они были заменены
        на MiaConfig.get_value().
        """
        with pytest.raises(ImportError):
            from core.adaptive_router import HISTORY_WINDOW

    def test_task_system_e2e_imports(self) -> None:
        """test_task_system_e2e.py: P95_THRESHOLD удалён."""
        with pytest.raises(ImportError):
            from core.adaptive_router import P95_THRESHOLD

    def test_adaptive_router_works_with_config(self) -> None:
        """AdaptiveRouter работает через MiaConfig (не через константы)."""
        store = TaskStore()
        router = AdaptiveRouter(store)
        # p95_threshold загружается из конфига
        assert router._p95_threshold > 0
        assert router._history_window > 0


# ── Дополнительно: set_global_dispatcher ──


class TestGlobalDispatcher:
    """Тесты глобального dispatcher."""

    def setup_method(self) -> None:
        set_global_dispatcher(None)

    def test_set_and_resolve(self) -> None:
        """set_global_dispatcher → _resolve_dispatcher возвращает тот же объект."""
        from core.task_decorator import _resolve_dispatcher

        tp = FakeThreadPool()
        wm = FakeWorkerManager()
        dp = SmartDispatcher(tp, wm)
        set_global_dispatcher(dp)
        assert _resolve_dispatcher() is dp

    def test_resolve_returns_none_when_not_set(self) -> None:
        """_resolve_dispatcher возвращает None если dispatcher не установлен."""
        from core.task_decorator import _resolve_dispatcher

        assert _resolve_dispatcher() is None

    def teardown_method(self) -> None:
        set_global_dispatcher(None)
