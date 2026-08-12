"""State Manager — центральный оркестратор.

Используй logging_config.setup_logging() при старте приложения.
"""
from typing import Any

from argenta_logging import get_logger

from api_proxy import ApiProxy
from cpu_affinity import CpuAffinityProvider
from errors import MiaError, ModuleNotFoundError, ModuleLoadError
from event_bus import EventBus, EVENT_PROCESS_DIED
from heartbeat_monitor import HeartbeatMonitor
from module_base import ModuleBase
from module_manager import ModuleManager
from process_pool import ProcessPool
from thread_pool import ThreadPoolManager

log = get_logger(__name__)


class State:
    """Главный класс — точка входа.

    Управляет загрузкой модулей, предоставляет доступ к API через state.api.

    Args:
        modules_dir: Путь к директории с модулями (по умолчанию "modules").
        allowed_modules: Whitelist имён модулей. Если None — разрешены все.
    """

    def __init__(self, modules_dir: str = "modules", allowed_modules: list[str] | None = None) -> None:
        self._modules_dir = modules_dir
        self._module_manager = ModuleManager(modules_dir, allowed_modules=allowed_modules)
        self._modules: dict[str, ModuleBase] = {}
        self._thread_pool = ThreadPoolManager()
        self._api_proxy = ApiProxy(self._thread_pool)
        self._event_bus = EventBus()
        self._cpu_affinity = CpuAffinityProvider()
        self._heartbeat_monitor = HeartbeatMonitor()
        self._process_pool: ProcessPool | None = None
        log.info("State created", extra={"modules_dir": modules_dir})

    def load_module(self, name: str) -> None:
        """Загрузить модуль из modules/{name}/__init__.py.

        Args:
            name: Имя модуля.
        """
        if name in self._modules:
            log.warning("Module already loaded", extra={"module_name": name})
            return

        try:
            module = self._module_manager.load(name, state=self)
            self._modules[name] = module
            self._api_proxy.register_module(module)
            log.info("Module loaded into State", extra={"module_name": name, "version": module.version})
        except Exception as e:
            log.error("Failed to load module", extra={"module_name": name, "error": str(e)})
            raise ModuleLoadError(f"Failed to load module '{name}': {e}") from e

    def load_all_modules(self) -> None:
        """Автосканирование modules_dir и загрузка всех модулей."""
        discovered = self._module_manager.discover()
        log.info("Auto-scanning modules", extra={"count": len(discovered)})

        for name in discovered:
            if name not in self._modules:
                try:
                    self.load_module(name)
                except Exception as e:
                    log.error("Failed to load module during scan", extra={"module_name": name, "error": str(e)})

    def unload_module(self, name: str) -> None:
        """Выгрузить модуль (вызвать on_unload()).

        Args:
            name: Имя модуля для выгрузки.
        """
        if name not in self._modules:
            log.warning("Module not loaded", extra={"module_name": name})
            return

        try:
            self._module_manager.unload(name)
            del self._modules[name]
            self._api_proxy.unregister_module(name)
            log.info("Module unloaded from State", extra={"module_name": name})
        except Exception as e:
            log.error("Failed to unload module", extra={"module_name": name, "error": str(e)})

    def startup(self) -> None:
        """Инициализация: запуск пула потоков и heartbeat monitor."""
        self._thread_pool.start()
        self._heartbeat_monitor.start()
        log.info("State startup complete")

    @property
    def api(self) -> ApiProxy:
        """Вернуть ApiProxy для state.api.module.method()."""
        return self._api_proxy

    @property
    def event_bus(self) -> EventBus:
        """Вернуть EventBus для pub/sub коммуникации."""
        return self._event_bus

    @property
    def thread_pool(self) -> ThreadPoolManager:
        """Вернуть ThreadPoolManager."""
        return self._thread_pool

    @property
    def cpu_affinity(self) -> CpuAffinityProvider:
        """Вернуть CpuAffinityProvider."""
        return self._cpu_affinity

    @property
    def heartbeat_monitor(self) -> HeartbeatMonitor:
        """Вернуть HeartbeatMonitor."""
        return self._heartbeat_monitor

    @property
    def process_pool(self) -> ProcessPool | None:
        """Вернуть ProcessPool (если создан)."""
        return self._process_pool

    def create_process_pool(self, num_processes: int | None = None) -> ProcessPool:
        """Создать и запустить пул процессов.

        Args:
            num_processes: Количество процессов (по умолчанию — все ядра).

        Returns:
            Созданный пул процессов.
        """
        if self._process_pool is not None:
            log.warning("ProcessPool already exists")
            return self._process_pool

        # Обработчик таймаута heartbeat — публикация event process.died
        def _on_process_died(pid: int) -> None:
            self._event_bus.publish(EVENT_PROCESS_DIED, {"pid": pid})
            log.warning("Process died (heartbeat timeout)", extra={"pid": pid})

        self._heartbeat_monitor.set_timeout_handler(_on_process_died)

        self._process_pool = ProcessPool(
            num_processes=num_processes,
            affinity_provider=self._cpu_affinity,
            heartbeat_monitor=self._heartbeat_monitor,
        )
        self._process_pool.start()
        log.info("ProcessPool created and started", extra={"num_processes": num_processes})
        return self._process_pool

    def shutdown(self) -> None:
        """Корректное завершение: остановить пул, выгрузить все модули."""
        log.info("State shutting down")
        if self._process_pool is not None:
            self._process_pool.shutdown()
            self._process_pool = None
        self._heartbeat_monitor.stop()
        self._thread_pool.shutdown()
        for name in list(self._modules.keys()):
            self.unload_module(name)
        log.info("State shutdown complete")
