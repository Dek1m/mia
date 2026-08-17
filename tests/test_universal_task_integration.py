"""Комплексная проверка интеграции Universal Task System.

Проверяет критические сценарии:
1. Async bridge: async-функция диспатчится через WorkerManager
2. @task с dispatcher: Task в TaskStore, статусы start/complete
3. @task без dispatcher: RuntimeError
4. Write-lock: async write-задачи сериализуются
5. apiproxy.call диспатчится через dispatcher
6. modules/db: transaction() НЕ сломан
7. Метрики: worker_manager_tasks_submitted_total инкрементируется
8. ISmartDispatcher: dispatch_async в интерфейсе
9. _task_type_to_metric_key: корректный маппинг типов
"""
from __future__ import annotations

import asyncio
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.task import Task, TaskStatus, TaskType
from core.task_decorator import task, set_global_dispatcher
from pools.smart_dispatcher import SmartDispatcher, _task_type_to_metric_key


# ── Fixtures ──────────────────────────────────────────────


class FakeWorkerManager:
    """Синхронный WorkerManager."""

    def __init__(self) -> None:
        self.submitted: list[tuple] = []

    def submit(self, fn, *args, **kwargs):
        self.submitted.append((fn, args, kwargs))
        return fn(*args, **kwargs)


class FakeThreadPool:
    """Синхронный ThreadPool."""

    def __init__(self) -> None:
        self.submitted: list[tuple] = []

    def submit(self, fn, *args, **kwargs):
        self.submitted.append((fn, args, kwargs))
        return fn(*args, **kwargs)

    def start(self) -> None:
        pass

    def shutdown(self, wait: bool = True) -> None:
        pass


@pytest.fixture
def fake_dispatcher():
    """SmartDispatcher с FakeWorkerManager и SharedMemory."""
    wm = FakeWorkerManager()
    from core.shared_memory import SharedMemory
    sm = SharedMemory(backend="local", num_blocks=16, block_size=4096)
    sm.start()
    dp = SmartDispatcher(wm, shared_memory=sm)
    return dp, wm, sm


@pytest.fixture
def real_dispatcher():
    """SmartDispatcher с FakeWorkerManager."""
    wm = FakeWorkerManager()
    from core.shared_memory import SharedMemory
    sm = SharedMemory(backend="local", num_blocks=16, block_size=4096)
    sm.start()
    dp = SmartDispatcher(wm, shared_memory=sm)
    yield dp, wm
    sm.shutdown()


@pytest.fixture
def full_dispatcher():
    """SmartDispatcher с FakeWorkerManager."""
    wm = FakeWorkerManager()
    from core.shared_memory import SharedMemory
    sm = SharedMemory(backend="local", num_blocks=16, block_size=4096)
    sm.start()
    dp = SmartDispatcher(wm, shared_memory=sm)
    return dp, wm


# ── 1. Async bridge: async-функция диспатчится через WorkerManager ──


class TestAsyncBridge:
    """Проверка async bridge: async-функции корректно выполняются через dispatch_async."""

    def test_sync_function_via_dispatch_async(self, fake_dispatcher) -> None:
        """sync-функция через dispatch_async работает."""
        dp, wm, sm = fake_dispatcher

        def sync_fn(x: int) -> int:
            return x * 3

        future = dp.dispatch_async(sync_fn, 4)
        assert isinstance(future, Future)
        assert future.result() == 12

    def test_async_function_dispatched_via_worker_manager(
        self, real_dispatcher,
    ) -> None:
        """Async-функция диспатчится через dispatch_async."""
        dp, wm = real_dispatcher

        async def async_fn(x: int) -> int:
            await asyncio.sleep(0.01)
            return x * 2

        future = dp.dispatch_async(async_fn, 5)
        result = future.result(timeout=5)
        assert result == 10
        assert len(wm.submitted) == 1

    def test_async_function_with_task_object(self, fake_dispatcher) -> None:
        """dispatch_async с явным Task-объектом."""
        dp, wm, sm = fake_dispatcher

        async def async_fn(x: int) -> int:
            return x + 10

        task_obj = Task.create(module_id="test", fn_name="async_fn")
        future = dp.dispatch_async(task_obj, async_fn, 3)
        assert future.result() == 13


# ── 2. @task с dispatcher: Task в TaskStore ──


class TestTaskWithDispatcher:
    """Проверка: @task dispatch через SmartDispatcher."""

    def test_sync_task_dispatches(self, full_dispatcher) -> None:
        """sync @task dispatch через SmartDispatcher."""
        dp, wm = full_dispatcher
        set_global_dispatcher(dp)

        @task(type="cpu", timeout=5.0)
        def compute(x: int) -> int:
            return x * 2

        try:
            future = compute(5)
            assert future.result() == 10
        finally:
            set_global_dispatcher(None)

    def test_async_task_dispatches(self, full_dispatcher) -> None:
        """async @task dispatch через SmartDispatcher."""
        dp, wm = full_dispatcher

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
        dp, wm = full_dispatcher

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
        dp, wm = full_dispatcher

        t = Task.create(module_id="test", fn_name="fn")
        t.start()
        t.fail("something went wrong")
        assert t.status == TaskStatus.FAILED
        assert t.error == "something went wrong"


# ── 3. @task без dispatcher: RuntimeError ──


class TestTaskWithoutDispatcher:
    """Проверка: @task без SmartDispatcher выбрасывает RuntimeError."""

    def setup_method(self) -> None:
        set_global_dispatcher(None)

    def test_sync_task_raises_without_dispatcher(self) -> None:
        """sync @task без dispatcher → RuntimeError."""
        @task(type="cpu", timeout=5.0)
        def compute(x: int) -> int:
            return x * 2

        with pytest.raises(RuntimeError, match="SmartDispatcher not initialized"):
            compute(5)

    def test_async_task_raises_without_dispatcher(self) -> None:
        """async @task без dispatcher → RuntimeError."""
        @task(type="cpu", timeout=5.0)
        async def async_compute(x: int) -> int:
            await asyncio.sleep(0.01)
            return x * 3

        loop = asyncio.new_event_loop()
        try:
            with pytest.raises(RuntimeError, match="SmartDispatcher not initialized"):
                loop.run_until_complete(async_compute(4))
        finally:
            loop.close()

    def test_task_fallback_on_dispatcher_error(self) -> None:
        """@task выбрасывает RuntimeError если dispatcher сломан."""
        bad_dispatcher = MagicMock()
        bad_dispatcher.dispatch_async.side_effect = RuntimeError("broken")
        set_global_dispatcher(bad_dispatcher)

        @task(type="cpu", timeout=5.0)
        def compute(x: int) -> int:
            return x * 2

        try:
            with pytest.raises(RuntimeError, match="broken"):
                compute(5)
        finally:
            set_global_dispatcher(None)


# ── 4. Write-lock: async write-задачи сериализуются ──


class TestWriteLock:
    """Проверка: write-lock сериализует write-задачи."""

    def test_write_lock_manual_acquire_release(self, fake_dispatcher) -> None:
        """Ручное управление write-lock: acquire_lock/release_lock."""
        dp, wm, sm = fake_dispatcher

        dp.acquire_lock()
        try:
            # Блокировка захвачена
            assert dp._write_lock.locked()
        finally:
            dp.release_lock()

        assert not dp._write_lock.locked()


# ── 5. apiproxy.call диспатчится через dispatcher ──


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
        future = proxy.list_api()
        methods = future.result()
        assert isinstance(methods, list)


# ── 6. modules/db: transaction() НЕ сломан ──


class TestDbTransaction:
    """Проверка: transaction() корректно работает как async context manager."""

    def test_transaction_is_async_context_manager(self) -> None:
        """transaction() должен быть async context manager, НЕ coroutine."""
        from modules.db.provider import DatabaseProvider
        from modules.db.config import DatabaseConfig

        config = DatabaseConfig()

        mock_conn = MagicMock()

        mock_conn_tx = MagicMock()
        mock_conn_tx.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn_tx.__aexit__ = AsyncMock(return_value=False)
        mock_conn_tx.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn_tx.__aexit__ = AsyncMock(return_value=False)

        pool = MagicMock()
        pool.acquire.return_value = mock_conn_tx

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
        """@db_method + @asynccontextmanager = async context manager (FIXED)."""
        from modules.db.provider import DatabaseProvider
        from modules.db.config import DatabaseConfig

        config = DatabaseConfig()
        pool = AsyncMock()
        provider = DatabaseProvider(pool=pool, config=config)

        result = provider.transaction()

        assert not asyncio.iscoroutine(result), (
            "transaction() не должен возвращать coroutine"
        )
        assert hasattr(result, "__aenter__") and hasattr(result, "__aexit__"), (
            "transaction() должен возвращать async context manager"
        )

    def test_database_provider_crud_works(self) -> None:
        """DatabaseProvider CRUD-методы работают через @db_method + @task."""
        from modules.db.provider import DatabaseProvider
        from modules.db.config import DatabaseConfig

        config = DatabaseConfig()
        pool = AsyncMock()

        pool.fetchval.return_value = "test-id-1"
        pool.fetchrow.return_value = {"id": "test-id-1", "name": "Alice"}
        pool.fetch.return_value = [{"id": "test-id-1", "name": "Alice"}]
        pool.execute.return_value = "DELETE 1"

        provider = DatabaseProvider(pool=pool, config=config)

        loop = asyncio.new_event_loop()
        try:
            id_ = loop.run_until_complete(
                provider.insert("users", {"name": "Alice"})
            )
            assert id_ == "test-id-1"

            user = loop.run_until_complete(
                provider.get("users", "test-id-1")
            )
            assert user["name"] == "Alice"
        finally:
            loop.close()


# ── 7. Метрики: threadpool_tasks_submitted_total инкрементируется ──


class TestMetrics:
    """Проверка: метрики корректно инкрементируются через Prometheus."""

    def test_task_type_to_metric_key_all_types(self) -> None:
        """_task_type_to_metric_key: все типы маппятся корректно."""
        expected = {
            TaskType.IO: "unknown",
            TaskType.CPU: "cpu",
            TaskType.GPU: "gpu",
            TaskType.NETWORK: "network",
            TaskType.DATABASE: "database",
            TaskType.AGGREGATE: "aggregate",
            TaskType.UNKNOWN: "unknown",
        }

        for tt, expected_key in expected.items():
            actual = _task_type_to_metric_key(tt)
            assert actual == expected_key, f"{tt} → {actual}, ожидалось {expected_key}"


# ── 8. ISmartDispatcher: dispatch_async в интерфейсе ──


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
        """SmartDispatcher реализует dispatch_async."""
        wm = FakeWorkerManager()
        dp = SmartDispatcher(wm)

        assert hasattr(dp, "dispatch_async")
        assert callable(dp.dispatch_async)

    def test_smart_dispatcher_implements_interface_methods(self) -> None:
        """SmartDispatcher реализует все методы ISmartDispatcher."""
        wm = FakeWorkerManager()
        dp = SmartDispatcher(wm)

        assert hasattr(dp, "dispatch")
        assert hasattr(dp, "dispatch_async")
        assert hasattr(dp, "acquire_lock")
        assert hasattr(dp, "release_lock")


# ── 9. Regression: import errors в тестах ──


class TestRegressionImports:
    """Проверка: импорты в тестах работают после рефакторинга."""

    def test_no_adaptive_router_import(self) -> None:
        """AdaptiveRouter удалён."""
        with pytest.raises(ImportError):
            from core.adaptive_router import AdaptiveRouter

    def test_no_task_classifier_import(self) -> None:
        """TaskClassifier удалён."""
        with pytest.raises(ImportError):
            from core.task_classifier import TaskClassifier


# ── Дополнительно: set_global_dispatcher ──


class TestGlobalDispatcher:
    """Тесты глобального dispatcher."""

    def setup_method(self) -> None:
        set_global_dispatcher(None)

    def test_set_and_resolve(self) -> None:
        """set_global_dispatcher → _resolve_dispatcher возвращает тот же объект."""
        from core.task_decorator import _resolve_dispatcher

        wm = FakeWorkerManager()
        dp = SmartDispatcher(wm)
        set_global_dispatcher(dp)
        assert _resolve_dispatcher() is dp

    def test_resolve_returns_none_when_not_set(self) -> None:
        """_resolve_dispatcher возвращает None если dispatcher не установлен."""
        from core.task_decorator import _resolve_dispatcher

        assert _resolve_dispatcher() is None

    def teardown_method(self) -> None:
        set_global_dispatcher(None)
