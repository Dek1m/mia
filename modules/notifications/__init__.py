"""Модуль уведомлений — пример использования EventBus."""
from modules_system.module_base import ModuleBase, ModuleMeta

MODULE_VERSION = "1.0.0"


class NotificationsModule(ModuleBase):
    """Модуль уведомлений — подписывается на события и выводит уведомления."""

    @property
    def name(self) -> str:
        return "notifications"

    @property
    def version(self) -> str:
        return MODULE_VERSION

    @property
    def meta(self) -> ModuleMeta:
        return ModuleMeta()

    def __init__(self) -> None:
        self._log = None

    def on_load(self, state: "Application") -> None:  # noqa: F821
        self._log = state.log
        self._state = state
        state.event_bus.subscribe("data.processed", self._on_data_processed)

    def on_unload(self) -> None:
        self._log.info("notifications_module_unloaded")
        self._log = None

    def _on_data_processed(self, data: object) -> None:
        self._log.info("data_processed", extra={"data": str(data)})
