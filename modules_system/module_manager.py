"""Менеджер модулей — автосканирование и загрузка."""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import sys
import threading
from pathlib import Path
from typing import Any

from argenta_logging import get_logger

from modules_system.module_base import ModuleBase
from modules_system.verification import (
    VerificationError,
    VerificationMode,
    compare_versions,
    compute_runtime_hashes,
    load_and_validate_manifest,
    verify_module,
)

log = get_logger(__name__)


class ModuleManager:
    """Сканирует директорию, загружает модули.

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
        self._dir = Path(modules_dir)
        self._loaded: dict[str, ModuleBase] = {}
        self._allowed = set(allowed_modules) if allowed_modules else None
        self._lock = threading.RLock()
        self._verification_mode = verification_mode
        log.info(
            "ModuleManager created",
            extra={
                "modules_dir": str(self._dir),
                "allowed": list(self._allowed or []),
                "verification_mode": verification_mode.value,
            },
        )

    @property
    def verification_mode(self) -> VerificationMode:
        """Текущий режим верификации."""
        return self._verification_mode

    def discover(self) -> list[str]:
        """Найти все папки с __init__.py в modules_dir.

        Returns:
            Список имён модулей (директорий).
        """
        with self._lock:
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
            state: Экземпляр Application для передачи в on_load.

        Returns:
            Загруженный экземпляр модуля.

        Raises:
            ImportError: Если модуль не может быть импортирован.
            AttributeError: Если в модуле нет класса-наследника ModuleBase.
            VerificationError: Если верификация не прошла в STRICT режиме.
        """
        with self._lock:
            if name in self._loaded:
                log.warning("Module already loaded", extra={"module_name": name})
                return self._loaded[name]

            # Проверка whitelist
            if self._allowed is not None and name not in self._allowed:
                log.error("Module not in whitelist", extra={"module_name": name})
                raise PermissionError(f"Module '{name}' is not in the whitelist")

            module_path = self._dir / name

            # Защита от path traversal
            safe_name = Path(name).name
            if safe_name != name:
                log.error("Path traversal detected", extra={"module_name": name})
                raise ValueError(f"Invalid module name: {name}")

            # Проверка что путь внутри разрешённой директории
            try:
                resolved_module = module_path.resolve()
                resolved_dir = self._dir.resolve()
                if not resolved_module.is_relative_to(resolved_dir):
                    log.error("Module path escapes modules directory", extra={"module_name": name})
                    raise ValueError(f"Module path escapes modules directory: {module_path}")
            except (OSError, ValueError) as e:
                log.error("Path resolution failed", extra={"module_name": name, "error": str(e)})
                raise

            if not module_path.exists():
                log.error("Module directory not found", extra={"module_name": name, "path": str(module_path)})
                raise FileNotFoundError(f"Module directory not found: {module_path}")

            # Проверка на symlink
            if module_path.is_symlink():
                log.error("Module directory is a symlink", extra={"module_name": name})
                raise ValueError(f"Module directory is a symlink: {module_path}")

            init_file = module_path / "__init__.py"
            if not init_file.exists():
                log.error("Module __init__.py not found", extra={"module_name": name})
                raise FileNotFoundError(f"Module __init__.py not found: {init_file}")

            # Проверка размера __init__.py (защита от подмены)
            from core.config import MiaConfig
            max_init_size = MiaConfig.get().get_value("modules.max_init_size", 1_000_000)
            file_size = init_file.stat().st_size
            if file_size > max_init_size:
                log.error("Module __init__.py is suspiciously large", extra={"module_name": name, "size": file_size})
                raise ValueError(f"Module __init__.py too large: {file_size} bytes")

            # Читаем __init__.py ОДИН РАЗ — для хеша и для исполнения (TOCTOU защита)
            file_bytes = init_file.read_bytes()
            file_hash = hashlib.sha256(file_bytes).hexdigest()
            log.info(
                "Importing module",
                extra={"module_name": name, "file_hash": file_hash, "file_size": file_size},
            )

            # Хеш-верификация (до exec — проверка целостности файлов)
            verification_metadata = self._verify_module_integrity(name, module_path)

            # Импорт модуля
            full_module_name = f"modules.{name}"
            try:
                spec = importlib.util.spec_from_file_location(full_module_name, init_file)
                if spec is None or spec.loader is None:
                    raise ImportError(f"Cannot create module spec for {name}")
                module = importlib.util.module_from_spec(spec)
                sys.modules[full_module_name] = module

                # TOCTOU защита: выполняем прочитанные байты напрямую.
                # spec.loader.exec_module может прочитать файл заново — между
                # нашим read_bytes() и exec_module() файл мог измениться.
                # Поэтому компилируем и исполняем именно те байты, что уже проверили.
                code = compile(file_bytes, str(init_file), "exec")
                exec(code, module.__dict__)
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

            # Сверка версий (после exec, до on_load).
            # SHA256-верификация (выше) защищает от подмены файлов.
            # Сверка версий — диагностика устаревшего манифеста:
            # если код обновлён без перегенерации hash.json, версии разойдутся.
            manifest_version = verification_metadata.get("version")
            if manifest_version is not None:
                runtime_version = instance.version
                sha256_passed = verification_metadata.get("verified", False)
                version_hint = compare_versions(name, manifest_version, runtime_version, sha256_passed)
                verification_metadata["version_code"] = runtime_version
                verification_metadata["version_manifest"] = manifest_version
                verification_metadata["hint"] = version_hint

                if version_hint is not None:
                    if self._verification_mode == VerificationMode.STRICT:
                        raise VerificationError(version_hint)
                    log.warning(version_hint, extra={"module_name": name})
            else:
                # hash.json отсутствует или DISABLED — без сверки версий
                verification_metadata["version_code"] = instance.version
                verification_metadata["version_manifest"] = None
                verification_metadata["hint"] = None

            # Вызов on_load
            try:
                instance.on_load(state)
                log.info("Module on_load called", extra={"module_name": name, "version": instance.version})
            except Exception as e:
                log.error("Module on_load failed", extra={"module_name": name, "error": str(e)})
                raise

            # Установка метаданных верификации
            instance._verification_metadata = verification_metadata

            self._loaded[name] = instance
            log.info("Module loaded", extra={"module_name": name})
            return instance

    def _verify_module_integrity(self, name: str, module_path: Path) -> dict[str, Any]:
        """Выполнить хеш-верификацию модуля.

        Args:
            name: Имя модуля.
            module_path: Путь к директории модуля.

        Returns:
            Словарь метаданных верификации для установки на instance.

        Raises:
            VerificationError: Если STRICT и верификация не прошла.
        """
        if self._verification_mode == VerificationMode.DISABLED:
            return {"manifest_hash": None, "version": None, "verified": False, "mode": "disabled"}

        try:
            manifest = load_and_validate_manifest(module_path)
        except VerificationError as e:
            if self._verification_mode == VerificationMode.STRICT:
                raise
            log.warning("Manifest validation failed, continuing anyway", extra={"module_name": name, "error": str(e)})
            return {"manifest_hash": None, "version": None, "verified": False, "mode": self._verification_mode.value}

        if manifest is None:
            # hash.json отсутствует
            if self._verification_mode == VerificationMode.STRICT:
                raise VerificationError(f"Модуль '{name}': hash.json отсутствует (STRICT режим)")
            log.warning("hash.json not found, continuing anyway", extra={"module_name": name})
            return {"manifest_hash": None, "version": None, "verified": False, "mode": self._verification_mode.value}

        result = verify_module(module_path, manifest)

        if result.passed:
            log.info(
                "Module verification passed",
                extra={"module_name": name, "version": manifest.version, "files_count": len(manifest.files)},
            )
            return {
                "manifest_hash": manifest.manifest_hash,
                "version": manifest.version,
                "verified": True,
                "mode": self._verification_mode.value,
            }
        elif self._verification_mode == VerificationMode.STRICT:
            raise VerificationError(
                f"Модуль '{name}': верификация не прошла:\n" + "\n".join(result.mismatches)
            )
        else:
            log.warning(
                "Module verification failed (WARN mode), continuing anyway",
                extra={"module_name": name, "mismatches": result.mismatches},
            )
            return {
                "manifest_hash": manifest.manifest_hash,
                "version": manifest.version,
                "verified": False,
                "mode": self._verification_mode.value,
            }

    def unload(self, name: str) -> None:
        """Вызвать on_unload(), удалить из _loaded.

        Args:
            name: Имя модуля для выгрузки.
        """
        with self._lock:
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
        with self._lock:
            return self._loaded.get(name)

    def list_all(self) -> list[str]:
        """Вернуть список загруженных модулей.

        Returns:
            Список имён загруженных модулей.
        """
        with self._lock:
            return list(self._loaded.keys())
