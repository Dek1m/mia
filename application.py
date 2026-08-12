"""Application — главный класс, Composition Root для Mia.

Собирает все зависимости через DI, управляет lifecycle.
Заменяет State как точку входа.
"""
from __future__ import annotations

import threading
from typing import Any
from argenta_logging import get_logger
from interfaces import (
    ICache, IThreadPool, IProcessPool, IEventBus,
    IHeartbeatMonitor, IModuleRegistry, IServiceProvider,
)
from cache_interface import NullCache
from service_registry import ServiceRegistry
from module_registry import ModuleRegistry
from api_proxy import ApiProxy
from factories import (
    CacheFactory, ThreadPoolFactory,
    EventBusFactory, HeartbeatFactory,
)
from shutdown_manager import ShutdownManager
from errors import ModuleLoadError

log = get_logger(__name__)


class Application:
    """Главный класс Mia — Composition Root.

    Собирает все компоненты, управляет lifecycle.

    Пример:
        app = Application(modules_dir="modules")
        app.startup()
        app.load_module("sample")
        result = app.api.sample.add(1, 2)
        app.shutdown()
    """

    def __init__(
        self,
        modules_dir: str = "modules",
        allowed_modules: list[str] | None = None,
        cache_backend: str = "null",
    ) -> None:
        self._modules_dir = modules_dir
        self._lock = threading.RLock()

        # Service Registry (DI)
        self._services = ServiceRegistry()

        # Cache
        cache = CacheFactory.create(cache_backend)
        self._services.register(ICache, cache)

        # Thread Pool
        thread_pool = ThreadPoolFactory.create()
        self._services.register(IThreadPool, thread_pool)

        # Event Bus
        event_bus = EventBusFactory.create()
        self._services.register(IEventBus, event_bus)

        # Heartbeat Monitor
        heartbeat = HeartbeatFactory.create()
        self._services.register(IHeartbeatMonitor, heartbeat)

        # Module Registry
        module_registry = ModuleRegistry(modules_dir, allowed_modules)
        self._services.register(IModuleRegistry, module_registry)

        # API Proxy
        self._api_proxy = ApiProxy(thread_pool)

        # Shutdown Manager
        self._shutdown_manager = ShutdownManager()

        # Process Pool (lazy)
        self._process_pool: IProcessPool | None = None

        log.info("Application created", extra={"modules_dir": modules_dir})

    # === Properties ===

    @property
    def api(self) -> ApiProxy:
        """Доступ к API модулей."""
        return self._api_proxy

    @property
    def cache(self) -> ICache:
        """Доступ к кешу."""
        return self._services.resolve(ICache)

    @property
    def event_bus(self) -> IEventBus:
        """Доступ к шине событий."""
        return self._services.resolve(IEventBus)

    @property
    def thread_pool(self) -> IThreadPool:
        """Доступ к пулу потоков."""
        return self._services.resolve(IThreadPool)

    @property
    def heartbeat(self) -> IHeartbeatMonitor:
        """Доступ к монитору heartbeat."""
        return self._services.resolve(IHeartbeatMonitor)

    @property
    def heartbeat_monitor(self) -> IHeartbeatMonitor:
        """Доступ к монитору heartbeat (alias для обратной совместимости)."""
        return self._services.resolve(IHeartbeatMonitor)

    @property
    def modules(self) -> IModuleRegistry:
        """Доступ к реестру модулей."""
        return self._services.resolve(IModuleRegistry)

    @property
    def _modules(self) -> dict[str, Any]:
        """Доступ к dict модулей (для обратной совместимости со State)."""
        return self._services.resolve(IModuleRegistry)._modules

    @property
    def process_pool(self) -> IProcessPool | None:
        """Доступ к пулу процессов."""
        return self._process_pool

    @property
    def cpu_affinity(self) -> Any:
        """Доступ к провайдеру CPU affinity."""
        from cpu_affinity import CpuAffinityProvider
        return CpuAffinityProvider()

    @property
    def services(self) -> IServiceProvider:
        """Доступ к DI контейнеру."""
        return self._services

    # === Lifecycle ===

    def startup(self) -> None:
        """Инициализация: запуск потоков, heartbeat."""
        self._services.resolve(IThreadPool).start()
        self._services.resolve(IHeartbeatMonitor).start()
        log.info("Application startup complete")

    def shutdown(self) -> None:
        """Корректное завершение."""
        log.info("Application shutting down")

        if self._process_pool is not None:
            self._process_pool.shutdown()
            self._process_pool = None

        self._services.resolve(IHeartbeatMonitor).stop()
        self._services.resolve(IThreadPool).shutdown()

        # Выгрузить все модули
        registry = self._services.resolve(IModuleRegistry)
        for name in registry.list_all():
            self.unload_module(name)

        log.info("Application shutdown complete")

    # === Module Management ===

    def load_module(self, name: str) -> None:
        """Загрузить модуль."""
        registry = self._services.resolve(IModuleRegistry)
        try:
            module = registry.load(name, state=self)
            self._api_proxy.register_module(module)
            log.info("Module loaded", extra={"module_name": name})
        except Exception as e:
            log.error("Failed to load module", extra={"module_name": name, "error": str(e)})
            raise ModuleLoadError(f"Failed to load module '{name}': {e}") from e

    def load_all_modules(self) -> None:
        """Автозагрузка всех модулей."""
        registry = self._services.resolve(IModuleRegistry)
        discovered = registry.discover()
        log.info("Auto-scanning modules", extra={"count": len(discovered)})

        for name in discovered:
            try:
                self.load_module(name)
            except Exception as e:
                log.error("Failed to load module", extra={"module_name": name, "error": str(e)})

    def unload_module(self, name: str) -> None:
        """Выгрузить модуль."""
        registry = self._services.resolve(IModuleRegistry)
        try:
            registry.unload(name)
            self._api_proxy.unregister_module(name)
            log.info("Module unloaded", extra={"module_name": name})
        except Exception as e:
            log.error("Failed to unload module", extra={"module_name": name, "error": str(e)})

    # === Process Pool ===

    def create_process_pool(self, num_processes: int | None = None) -> IProcessPool:
        """Создать пул процессов."""
        with self._lock:
            if self._process_pool is not None:
                return self._process_pool

            from process_pool import ProcessPool
            from cpu_affinity import CpuAffinityProvider

            affinity = CpuAffinityProvider()
            heartbeat = self._services.resolve(IHeartbeatMonitor)

            def _on_process_died(pid: int) -> None:
                self._services.resolve(IEventBus).publish("process.died", {"pid": pid})

            heartbeat.set_timeout_handler(_on_process_died)

            self._process_pool = ProcessPool(
                num_processes=num_processes,
                affinity_provider=affinity,
                heartbeat_monitor=heartbeat,
            )
            self._process_pool.start()
            log.info("ProcessPool created", extra={"num_processes": num_processes})
            return self._process_pool

    # === Cache ===

    def set_cache(self, cache: ICache) -> None:
        """Установить кеш-бэкенд."""
        self._services.register(ICache, cache)
        log.info("Cache backend set", extra={"type": type(cache).__name__})
