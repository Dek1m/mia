"""Модуль уведомлений — пример использования EventBus."""
from module_base import ModuleBase


class NotificationsModule(ModuleBase):
    """Модуль уведомлений — подписывается на события и выводит уведомления."""

    @property
    def name(self) -> str:
        return "notifications"

    def on_load(self, state: "Application") -> None:  # noqa: F821
        self._state = state
        state.event_bus.subscribe("data.processed", self._on_data_processed)

    def _on_data_processed(self, data: object) -> None:
        print(f"Notification: data processed! {data}")