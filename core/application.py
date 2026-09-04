"""Application — Composition Root. По умолчанию @task уходит в Redis-очередь."""
from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
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
from modules_system.module_base import ModuleMeta, should_load
from modules_system.module_registry import ModuleRegistry
from modules_system.verification import VerificationMode
from resilience.shutdown_manager import ShutdownManager

log = get_logger(__name__)

_REST_MODULE = "rest"
_APIPROXY_MODULE = "apiproxy"


def process_role_from_env() -> str:
    """Роль процесса: SERVICE_NAME belle→api, belle-worker→worker, иначе all."""
    service = os.environ.get("SERVICE_NAME", "").strip()
    if service == "belle":
        return "api"
    if service == "belle-worker":
        return "worker"
    return "all"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
        self._runtime_registry: Any | None = None
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

    def set_runtime_registry(self, registry: Any) -> None:
        """Redis-снимок runtime. None — не публиковать."""
        self._runtime_registry = registry

    def publish_runtime(self) -> None:
        """Полный HASH текущего процесса + heartbeat."""
        registry = self._runtime_registry
        if registry is None:
            return
        snapshots = [
            self._runtime_snapshot(name, "loaded", "ok", None, self._module_meta(name))
            for name in self.modules.list_all()
        ]
        publish = getattr(registry, "publish_all", None)
        if callable(publish):
            publish(snapshots)

    def _runtime_snapshot(
        self,
        name: str,
        status: str,
        health: str,
        error: str | None,
        meta: ModuleMeta,
    ) -> dict[str, Any]:
        module = self.modules.get(name)
        registry = self._runtime_registry
        return {
            "name": name,
            "display_name": meta.display_name or name,
            "version": getattr(module, "version", "0.0.0") if module is not None else "0.0.0",
            "status": status,
            "health": health,
            "load_on": meta.load_on,
            "is_system": meta.is_system,
            "is_example": meta.is_example,
            "source": "image",
            "error": error,
            "pid": os.getpid(),
            "service": getattr(registry, "service", ""),
            "updated_at": _utc_now(),
        }

    def load_all_modules(self, role: str | None = None) -> None:
        """discover → should_load → все кроме rest → collect → rest last."""
        resolved = role if role is not None else process_role_from_env()
        registry = self._services.resolve(IModuleRegistry)
        queue = self._queue_for_role(registry.discover_and_sort(), resolved)
        rest_last = _REST_MODULE in queue
        phase = [name for name in queue if name != _REST_MODULE]
        log.info(
            "Auto-scanning modules",
            extra={"count": len(queue), "role": resolved, "order": phase + ([_REST_MODULE] if rest_last else [])},
        )
        for name in phase:
            self._load_discovered(name)
        self._collect_apiproxy()
        if rest_last:
            self._load_discovered(_REST_MODULE)
        self._apply_pref_overlay()

    def _apply_pref_overlay(self) -> None:
        """system.pref → живые конфиги после полной очереди load."""
        system = self.modules.get("system")
        provider = getattr(system, "_provider", None) if system is not None else None
        repo = getattr(provider, "_repo", None) if provider is not None else None
        database = getattr(repo, "_database", None) if repo is not None else None
        if database is None:
            return
        try:
            from modules.system.prefs import apply_stored

            rows = database.fetch("SELECT key, value FROM system.pref") or []
            apply_stored(self, [dict(row) for row in rows])
        except Exception as exc:
            log.warning("pref_overlay_skipped", extra={"error": str(exc)})

    def _queue_for_role(self, discovered: list[str], role: str) -> list[str]:
        selected: list[str] = []
        for name in discovered:
            meta = self._module_meta(name)
            if role == "all":
                if meta.is_example:
                    continue
            elif not should_load(meta, role):
                continue
            selected.append(name)
        return selected

    def _module_meta(self, name: str) -> ModuleMeta:
        registry = self._services.resolve(IModuleRegistry)
        reader = getattr(registry, "read_meta", None)
        if callable(reader):
            return reader(name)
        return ModuleMeta()

    def _load_discovered(self, name: str) -> None:
        meta = self._module_meta(name)
        try:
            self.load_module(name)
            self._publish_runtime(name, "loaded", "ok", None, meta)
        except Exception as exc:
            log.error("Failed to load module", extra={"module_name": name, "error": str(exc)})
            self._publish_runtime(name, "failed", "degraded", str(exc), meta)
            if meta.is_system:
                raise

    def _publish_runtime(
        self,
        name: str,
        status: str,
        health: str,
        error: str | None,
        meta: ModuleMeta,
    ) -> None:
        registry = self._runtime_registry
        if registry is None:
            return
        upsert = getattr(registry, "upsert", None)
        if callable(upsert):
            upsert(self._runtime_snapshot(name, status, health, error, meta))

    def _collect_apiproxy(self) -> None:
        """Повторный collect после фазы 1 — apiproxy видит модули после своего on_load."""
        proxy = self.modules.get(_APIPROXY_MODULE)
        if proxy is None:
            return
        provider = getattr(proxy, "_provider", None)
        method_registry = getattr(provider, "registry", None) if provider is not None else None
        collect = getattr(method_registry, "collect_from_module", None)
        if collect is None:
            return
        for name in self.modules.list_all():
            if name in (_REST_MODULE, _APIPROXY_MODULE):
                continue
            module = self.modules.get(name)
            target = getattr(module, "_provider", None) if module is not None else None
            if target is None:
                continue
            try:
                collect(target, name)
            except Exception as exc:
                log.warning(
                    "failed_to_collect_methods",
                    extra={"module_name": name, "error": str(exc)},
                )

    def apply_schemas(self) -> None:
        """Накат DDL загруженных модулей в topo-порядке. Только migrate."""
        registry = self._services.resolve(IModuleRegistry)
        names = registry.list_all()
        log.info("apply_schemas_start", extra={"modules": names})
        for name in names:
            module = registry.get(name)
            if module is None:
                continue
            apply = getattr(module, "apply_schema", None)
            if apply is None:
                continue
            log.info("apply_schema", extra={"module_name": name})
            apply(self)
        log.info("apply_schemas_done", extra={"modules": names})

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
