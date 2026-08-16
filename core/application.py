"""Application — главный класс, Composition Root для Mia.

Собирает все зависимости через DI, управляет lifecycle.
Заменяет State как точку входа.
"""
from __future__ import annotations

import os
import threading
from typing import Any
from argenta_logging import get_logger
from core.interfaces import (
    ICache, IThreadPool, IEventBus,
    IHeartbeatMonitor, IModuleRegistry, IServiceProvider,
    ICpuMetricsCollector, ILoadBalancer, IWorkerManager, IDatabase,
)
from storage.cache_interface import NullCache
from core.service_registry import ServiceRegistry
from modules_system.module_registry import ModuleRegistry
from modules_system.verification import VerificationMode
from communication.api_proxy import ApiProxy
from core.factories import (
    CacheFactory, ThreadPoolFactory,
    EventBusFactory, HeartbeatFactory,
    CpuMetricsCollectorFactory, LoadBalancerFactory, WorkerManagerFactory,
    DatabaseFactory,
)
from pools.smart_dispatcher import SmartDispatcher
from resilience.shutdown_manager import ShutdownManager
from core.errors import ModuleLoadError

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
        modules_dir: str | None = None,
        allowed_modules: list[str] | None = None,
        cache_backend: str | None = None,
        verification_mode: VerificationMode | None = None,
    ) -> None:
        """Создать Application.

        Приоритет verification_mode:
        1. Явный параметр verification_mode (если передан).
        2. ENV MIA_MODULE_VERIFICATION ("strict"|"warn"|"disabled").
        3. Файл mia.json5 (modules.verification.mode).
        4. Дефолт: DISABLED.

        Args:
            modules_dir: Директория с модулями (None → config → "modules").
            allowed_modules: Whitelist модулей (None = все).
            cache_backend: Тип кеша (None → config → "null").
            verification_mode: Режим верификации модулей.
                None → ENV → config → disabled.
        """
        # Загрузка конфига (если ещё не загружен)
        from core.config import MiaConfig
        config = MiaConfig.get()

        self._modules_dir = modules_dir or config.get_value("modules.dir", "modules")
        self._cache_backend = cache_backend or config.get_value("storage.cache.backend", "null")
        self._verification_mode = self._resolve_verification_mode(config, verification_mode)
        self._lock = threading.RLock()

        # Реестры верификации модулей
        # module_versions: {имя_модуля: "version:manifest_hash"}
        self._module_versions: dict[str, str] = {}
        # module_verification: {имя_модуля: True/False} — прошёл ли модуль верификацию
        self._module_verification: dict[str, bool] = {}

        # Service Registry (DI)
        self._services = ServiceRegistry()

        # Cache
        cache = CacheFactory.create(self._cache_backend)
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

        # CPU Metrics Collector
        cpu_metrics = CpuMetricsCollectorFactory.create()
        self._services.register(ICpuMetricsCollector, cpu_metrics)

        # Load Balancer
        load_balancer = LoadBalancerFactory.create()
        self._services.register(ILoadBalancer, load_balancer)

        # Worker Manager
        worker_manager = WorkerManagerFactory.create(
            load_balancer=load_balancer,
            heartbeat_monitor=heartbeat,
        )
        self._services.register(IWorkerManager, worker_manager)

        # Database + SmartDispatcher + Task System
        smart_dispatcher = SmartDispatcher(thread_pool, worker_manager)
        database, task_store, stats_writer = DatabaseFactory.create_with_task_system(
            cache=cache, dispatcher=smart_dispatcher,
        )
        self._services.register(IDatabase, database)

        self._smart_dispatcher = smart_dispatcher
        self._task_store = task_store
        self._stats_writer = stats_writer

        # Module Registry
        module_registry = ModuleRegistry(self._modules_dir, allowed_modules, verification_mode=self._verification_mode)
        self._services.register(IModuleRegistry, module_registry)

        # API Proxy
        self._api_proxy = ApiProxy(thread_pool)

        # Shutdown Manager
        self._shutdown_manager = ShutdownManager()

        log.info("Application created", extra={"modules_dir": self._modules_dir, "verification_mode": self._verification_mode.value})

    # === Properties ===

    @staticmethod
    def _resolve_verification_mode(config: Any, param: VerificationMode | None) -> VerificationMode:
        """Определить режим верификации: параметр > ENV > файл > дефолт DISABLED.

        Каскад:
        1. Явный параметр (если передан).
        2. ENV MIA_MODULE_VERIFICATION.
        3. Файл mia.json5 (modules.verification.mode).
        4. DISABLED.

        Args:
            config: MiaConfig instance.
            param: Явно переданный параметр (None = не задан).

        Returns:
            VerificationMode для использования.
        """
        # Если параметр задан явно — он главнее
        if param is not None:
            return param

        # Читаем ENV
        env_value = os.getenv("MIA_MODULE_VERIFICATION", "").strip().lower()
        if env_value:
            try:
                return VerificationMode.from_str(env_value)
            except ValueError:
                log.warning(
                    "Неизвестное значение MIA_MODULE_VERIFICATION, используется DISABLED",
                    extra={"env_value": env_value},
                )

        # Читаем из конфиг-файла
        file_value = config.get_value("modules.verification.mode", "disabled")
        if isinstance(file_value, str):
            try:
                return VerificationMode.from_str(file_value)
            except ValueError:
                log.warning(
                    "Неизвестное значение modules.verification.mode, используется DISABLED",
                    extra={"config_value": file_value},
                )

        return VerificationMode.DISABLED

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
    def modules(self) -> IModuleRegistry:
        """Доступ к реестру модулей."""
        return self._services.resolve(IModuleRegistry)

    @property
    def worker_manager(self) -> IWorkerManager:
        """Доступ к менеджеру воркеров."""
        return self._services.resolve(IWorkerManager)

    @property
    def cpu_metrics(self) -> ICpuMetricsCollector:
        """Доступ к метрикам CPU."""
        return self._services.resolve(ICpuMetricsCollector)

    @property
    def load_balancer(self) -> ILoadBalancer:
        """Доступ к балансировщику."""
        return self._services.resolve(ILoadBalancer)

    @property
    def database(self) -> IDatabase:
        """Доступ к Database."""
        return self._services.resolve(IDatabase)

    @property
    def smart_dispatcher(self) -> SmartDispatcher:
        """Доступ к SmartDispatcher."""
        return self._smart_dispatcher

    @property
    def task_store(self) -> Any:
        """Доступ к TaskStore."""
        return self._task_store

    @property
    def stats_writer(self) -> Any:
        """Доступ к StatsBatchWriter."""
        return self._stats_writer

    @property
    def services(self) -> IServiceProvider:
        """Доступ к DI контейнеру."""
        return self._services

    @property
    def verification_mode(self) -> VerificationMode:
        """Текущий режим верификации модулей."""
        return self._verification_mode

    @property
    def module_versions(self) -> dict[str, str]:
        """Реестр версий модулей: {имя: 'version:manifest_hash'}.

        Заполняется при загрузке модуля. Формат значения: 'v:hash'.
        """
        return self._module_versions

    @property
    def module_verification(self) -> dict[str, bool]:
        """Реестр результатов верификации: {имя: True/False}.

        True — модуль прошёл SHA256-верификацию, False — нет (warn/disabled/error).
        """
        return self._module_verification

    # === Lifecycle ===

    def startup(self) -> None:
        """Инициализация: запуск потоков, heartbeat, CPU metrics, воркеров, task system."""
        self._services.resolve(IThreadPool).start()
        self._services.resolve(IHeartbeatMonitor).start()
        self._services.resolve(ICpuMetricsCollector).start()

        # АВТОЗАПУСК ВОРКЕРОВ — по числу ядер CPU
        worker_manager = self._services.resolve(IWorkerManager)
        worker_manager.start()

        # Запуск StatsBatchWriter (фоновый flush статистики задач)
        self._stats_writer.start()

        log.info("Application startup complete")

    def shutdown(self) -> None:
        """Корректное завершение."""
        log.info("Application shutting down")

        # Остановить StatsBatchWriter (финальный flush)
        self._stats_writer.stop()

        # Остановить CPU metrics
        self._services.resolve(ICpuMetricsCollector).stop()

        # Остановить воркеров
        self._services.resolve(IWorkerManager).stop()

        self._services.resolve(IHeartbeatMonitor).stop()
        self._services.resolve(IThreadPool).shutdown()

        # Shutdown database
        self._services.resolve(IDatabase).shutdown()

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

            # Заполнение реестров верификации из _verification_metadata
            meta = getattr(module, "_verification_metadata", None)
            if meta is not None:
                version = meta.get("version") or "unknown"
                manifest_hash = meta.get("manifest_hash") or "none"
                self._module_versions[name] = f"{version}:{manifest_hash}"
                self._module_verification[name] = meta.get("verified", False)
            else:
                self._module_versions[name] = "unknown:none"
                self._module_verification[name] = False

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

    # === Cache ===

    def set_cache(self, cache: ICache) -> None:
        """Установить кеш-бэкенд."""
        self._services.register(ICache, cache)
        log.info("Cache backend set", extra={"type": type(cache).__name__})
