"""Менеджер модулей — автосканирование и загрузка."""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import sys
from pathlib import Path
from typing import Any

from argenta_logging import get_logger

from module_base import ModuleBase

log = get_logger(__name__)


class ModuleManager:
    """Сканирует директорию, загружает модули.

    Args:
        modules_dir: Путь к директории с модулями.
        allowed_modules: Whitelist имён модулей. Если None — разрешены все.
    """

    def __init__(self, modules_dir: str, allowed_modules: list[str] | None = None) -> None:
        self._dir = Path(modules_dir)
        self._loaded: dict[str, ModuleBase] = {}
        self._allowed = set(allowed_modules) if allowed_modules else None
        log.info(
            "ModuleManager created",
            extra={"modules_dir": str(self._dir), "allowed": list(self._allowed or [])},
        )

    def discover(self) -> list[str]:
        """Найти все папки с __init__.py в modules_dir.

        Returns:
            Список имён модулей (директорий).
        """
        if not self._dir.exists():
            log.warning("Modules directory not found", extra={"path": str(self._dir)})
            return []

        modules: list[str] = []
        for item in self._dir.iterdir():
            if item.is_dir() and (item / "__init__.py").exists():
                if self._allowed is None or item.name in self._allowed:
                    modules.append(item.name)
                else:
                    log.warning("Module not in whitelist, skipping", extra={"module_name": item.name})
        log.debug("Discovered modules", extra={"count": len(modules), "modules": modules})
        return sorted(modules)

    def load(self, name: str, state: Any = None) -> ModuleBase:
        """Импортировать модуль, создать экземпляр, вызвать on_load().

        Args:
            name: Имя модуля (директория в modules_dir).
            state: Экземпляр State для передачи в on_load.

        Returns:
            Загруженный экземпляр модуля.

        Raises:
            ImportError: Если модуль не может быть импортирован.
            AttributeError: Если в модуле нет класса-наследника ModuleBase.
        """
        if name in self._loaded:
            log.warning("Module already loaded", extra={"module_name": name})
            return self._loaded[name]

        # Проверка whitelist
        if self._allowed is not None and name not in self._allowed:
            log.error("Module not in whitelist", extra={"module_name": name})
            raise PermissionError(f"Module '{name}' is not in the whitelist")

        module_path = self._dir / name
        if not module_path.exists():
            log.error("Module directory not found", extra={"module_name": name, "path": str(module_path)})
            raise FileNotFoundError(f"Module directory not found: {module_path}")

        init_file = module_path / "__init__.py"
        if not init_file.exists():
            log.error("Module __init__.py not found", extra={"module_name": name})
            raise FileNotFoundError(f"Module __init__.py not found: {init_file}")

        # Проверка размера __init__.py (защита от подмены)
        file_size = init_file.stat().st_size
        if file_size > 1_000_000:  # 1MB
            log.error("Module __init__.py is suspiciously large", extra={"module_name": name, "size": file_size})
            raise ValueError(f"Module __init__.py too large: {file_size} bytes")

        # Логирование хеша файла для аудита
        file_hash = hashlib.sha256(init_file.read_bytes()).hexdigest()[:16]
        log.info(
            "Importing module",
            extra={"module_name": name, "file_hash": file_hash, "file_size": file_size},
        )

        # Импорт модуля
        full_module_name = f"modules.{name}"
        try:
            spec = importlib.util.spec_from_file_location(full_module_name, init_file)
            if spec is None or spec.loader is None:
                raise ImportError(f"Cannot create module spec for {name}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[full_module_name] = module
            spec.loader.exec_module(module)
        except Exception as e:
            log.error("Failed to import module", extra={"module_name": name, "error": str(e)})
            raise ImportError(f"Failed to import module {name}: {e}") from e

        # Поиск класса-наследника ModuleBase
        module_class = None
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if (
                isinstance(attr, type)
                and issubclass(attr, ModuleBase)
                and attr is not ModuleBase
            ):
                module_class = attr
                break

        if module_class is None:
            log.error("No ModuleBase subclass found", extra={"module_name": name})
            raise AttributeError(f"No ModuleBase subclass found in module {name}")

        # Создание экземпляра
        try:
            instance = module_class()
            log.info("Module instantiated", extra={"module_name": name, "class": module_class.__name__})
        except Exception as e:
            log.error("Failed to instantiate module", extra={"module_name": name, "error": str(e)})
            raise

        # Вызов on_load
        try:
            instance.on_load(state)
            log.info("Module on_load called", extra={"module_name": name, "version": instance.version})
        except Exception as e:
            log.error("Module on_load failed", extra={"module_name": name, "error": str(e)})
            raise

        self._loaded[name] = instance
        log.info("Module loaded", extra={"module_name": name})
        return instance

    def unload(self, name: str) -> None:
        """Вызвать on_unload(), удалить из _loaded.

        Args:
            name: Имя модуля для выгрузки.
        """
        if name not in self._loaded:
            log.warning("Module not loaded", extra={"module_name": name})
            return

        instance = self._loaded[name]
        try:
            instance.on_unload()
            log.info("Module on_unload called", extra={"module_name": name})
        except Exception as e:
            log.error("Module on_unload failed", extra={"module_name": name, "error": str(e)})

        del self._loaded[name]
        log.info("Module unloaded", extra={"module_name": name})

    def get(self, name: str) -> ModuleBase | None:
        """Получить загруженный модуль по имени.

        Args:
            name: Имя модуля.

        Returns:
            Экземпляр модуля или None.
        """
        return self._loaded.get(name)

    def list_all(self) -> list[str]:
        """Вернуть список загруженных модулей.

        Returns:
            Список имён загруженных модулей.
        """
        return list(self._loaded.keys())