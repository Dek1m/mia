"""Application — Composition Root. По умолчанию @task уходит в Redis-очередь."""
from __future__ import annotations

import os
import threading
from typing import Any

from argenta_logging import get_logger
from communication.api_proxy import ApiProxy
from core.dispatch.local import LocalInvokeDispatcher
from core.errors import ModuleLoadError
from core.factories import CacheFactory, DatabaseFactory, EventBusFactory
from core.interfaces import (
    ICache,
    IDatabase,
    IEventBus,
    ILogger,
    IModuleRegistry,
    IServiceProvider,
    ISmartDispatcher,
)
from core.service_registry import ServiceRegistry
from modules_system.module_registry import ModuleRegistry
from modules_system.verification import VerificationMode
from resilience.shutdown_manager import ShutdownManager

log = get_logger(__name__)


def _default_dispatcher() -> ISmartDispatcher:
    """Очередь Redis (mia-worker), если не MIA_DISPATCH=local."""
    if os.environ.get("MIA_DISPATCH", "").strip().lower() == "local":
        log.info("dispatcher_local")
        return LocalInvokeDispatcher()
    from modules.worker.dispatcher import QueueDispatcher

    log.info("dispatcher_queue")
    return QueueDispatcher.from_config()


class Application:
    """Главный класс Mia — Composition Root.

    По умолчанию QueueDispatcher (Redis). MIA_DISPATCH=local — in-process.
    Можно передать dispatcher= явно.
    """

    def __init__(
        self,
        modules_dir: str | None = None,
        allowed_modules: list[str] | None = None,
        cache_backend: str | None = None,
        verification_mode: VerificationMode | None = None,
        dispatcher: ISmartDispatcher | None = None,
    ) -> None:
        from core.config import MiaConfig

        config = MiaConfig.get()
        self._modules_dir = modules_dir or config.get_value("modules.dir", "modules")
        self._cache_backend = cache_backend or config.get_value("storage.cache.backend", "null")
        self._verification_mode = self._resolve_verification_mode(config, verification_mode)
        self._lock = threading.RLock()
        self._module_versions: dict[str, str] = {}
        self._module_verification: dict[str, bool] = {}
        self._services = ServiceRegistry()

        cache = CacheFactory.create(self._cache_backend)
        self._services.register(ICache, cache)
        self._services.register(IEventBus, EventBusFactory.create())

        dispatcher = dispatcher or _default_dispatcher()
        self._services.register(ISmartDispatcher, dispatcher)
        from core.task_decorator import set_global_dispatcher

        set_global_dispatcher(dispatcher)

        database, _, stats_writer = DatabaseFactory.create_with_task_system(
            cache=cache, dispatcher=dispatcher,
        )
        self._services.register(IDatabase, database)
        self._smart_dispatcher = dispatcher
        self._stats_writer = stats_writer

        module_registry = ModuleRegistry(
            self._modules_dir, allowed_modules, verification_mode=self._verification_mode,
        )
        self._services.register(IModuleRegistry, module_registry)
        self._api_proxy = ApiProxy(dispatcher=dispatcher)

        from modules.log import LogModule

        log_module = LogModule()
        log_module.on_load(self)
        self._api_proxy.register_module(log_module)
        self._shutdown_manager = ShutdownManager()
        log.info(
            "Application created",
            extra={
                "modules_dir": self._modules_dir,
                "verification_mode": self._verification_mode.value,
            },
        )

    @staticmethod
    def _resolve_verification_mode(config: Any, param: VerificationMode | None) -> VerificationMode:
        if param is not None:
            return param
        env_value = os.getenv("MIA_MODULE_VERIFICATION", "").strip().lower()
        if env_value:
            try:
                return VerificationMode.from_str(env_value)
            except ValueError:
                log.warning(
                    "Неизвестное значение MIA_MODULE_VERIFICATION, используется DISABLED",
                    extra={"env_value": env_value},
                )
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
        return self._api_proxy

    @property
    def log(self) -> ILogger:
        return self._services.resolve(ILogger)

    @property
    def cache(self) -> ICache:
        return self._services.resolve(ICache)

    @property
    def event_bus(self) -> IEventBus:
        return self._services.resolve(IEventBus)

    @property
    def modules(self) -> IModuleRegistry:
        return self._services.resolve(IModuleRegistry)

    @property
    def database(self) -> IDatabase:
        return self._services.resolve(IDatabase)

    @property
    def smart_dispatcher(self) -> ISmartDispatcher:
        return self._smart_dispatcher

    @property
    def stats_writer(self) -> Any:
        return self._stats_writer

    @property
    def services(self) -> IServiceProvider:
        return self._services

    @property
    def verification_mode(self) -> VerificationMode:
        return self._verification_mode

    @property
    def module_versions(self) -> dict[str, str]:
        return self._module_versions

    @property
    def module_verification(self) -> dict[str, bool]:
        return self._module_verification

    def startup(self) -> None:
        """Не спавнит процессы. Только stats writer."""
        self._stats_writer.start()
        log.info("Application startup complete")

    def shutdown(self) -> None:
        log.info("Application shutting down")
        self._stats_writer.stop()
        self._services.resolve(IDatabase).shutdown()
        registry = self._services.resolve(IModuleRegistry)
        for name in registry.list_all():
            self.unload_module(name)
        log.info("Application shutdown complete")

    def load_module(self, name: str) -> None:
        registry = self._services.resolve(IModuleRegistry)
        try:
            module = registry.load(name, state=self)
            self._api_proxy.register_module(module)
            meta = getattr(module, "_verification_metadata", None)
            if meta is not None:
                version = meta.get("version") or "unknown"
                manifest_hash = meta.get("manifest_hash") or "none"
                self._module_versions[name] = f"{version}:{manifest_hash}"
                self._module_verification[name] = meta.get("verified", False)
            else:
                self._module_versions[name] = "unknown:none"
                self._module_verification[name] = False
        except Exception as e:
            log.error("Failed to load module", extra={"module_name": name, "error": str(e)})
            raise ModuleLoadError(f"Failed to load module '{name}': {e}") from e

    def load_all_modules(self) -> None:
        registry = self._services.resolve(IModuleRegistry)
        discovered = registry.discover_and_sort()
        log.info("Auto-scanning modules", extra={"count": len(discovered)})
        for name in discovered:
            try:
                self.load_module(name)
            except Exception as e:
                log.error("Failed to load module", extra={"module_name": name, "error": str(e)})

    def unload_module(self, name: str) -> None:
        registry = self._services.resolve(IModuleRegistry)
        try:
            registry.unload(name)
            self._api_proxy.unregister_module(name)
            log.info("Module unloaded", extra={"module_name": name})
        except Exception as e:
            log.error("Failed to unload module", extra={"module_name": name, "error": str(e)})

    def set_cache(self, cache: ICache) -> None:
        self._services.register(ICache, cache)
        log.info("Cache backend set", extra={"type": type(cache).__name__})
