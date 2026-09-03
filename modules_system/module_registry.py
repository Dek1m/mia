"""ModuleRegistry — реестр и загрузка модулей."""
from __future__ import annotations

import threading
from typing import Any

from argenta_logging import get_logger
from core.interfaces import IModuleRegistry
from modules_system.module_base import ModuleBase
from modules_system.module_manager import ModuleManager
from modules_system.verification import VerificationMode

log = get_logger(__name__)


class ModuleRegistry(IModuleRegistry):
    """Реестр модулей с thread-safety.

    Args:
        modules_dir: Путь к директории с модулями.
        allowed_modules: Whitelist имён модулей. Если None — разрешены все.
        verification_mode: Режим хеш-верификации модулей.
    """

    def __init__(
        self,
        modules_dir: str,
        allowed_modules: list[str] | None = None,
        verification_mode: VerificationMode = VerificationMode.STRICT,
    ) -> None:
        self._manager = ModuleManager(
            modules_dir,
            allowed_modules=allowed_modules,
            verification_mode=verification_mode,
        )
        self._modules: dict[str, ModuleBase] = {}
        self._lock = threading.RLock()

    @property
    def verification_mode(self) -> VerificationMode:
        """Текущий режим верификации."""
        return self._manager.verification_mode

    def discover(self) -> list[str]:
        return self._manager.discover()

    def discover_and_sort(self) -> list[str]:
        """Найти модули и отсортировать по зависимостям.

        Returns:
            Список имён модулей в топологическом порядке.
        """
        return self._manager.discover_and_sort()

    def read_meta(self, name: str) -> Any:
        """AST ModuleMeta без загрузки модуля."""
        return self._manager.read_meta(name)

    def load(self, name: str, state: Any = None) -> Any:
        with self._lock:
            if name in self._modules:
                return self._modules[name]
            module = self._manager.load(name, state=state)
            self._modules[name] = module
            return module

    def unload(self, name: str) -> None:
        with self._lock:
            if name in self._modules:
                self._manager.unload(name)
                del self._modules[name]

    def get(self, name: str) -> Any | None:
        with self._lock:
            return self._modules.get(name)

    def list_all(self) -> list[str]:
        with self._lock:
            return list(self._modules.keys())
