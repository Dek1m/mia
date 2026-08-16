"""Хеш-верификация модулей — проверка целостности файлов."""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class VerificationMode(StrEnum):
    """Режимы верификации модулей.

    STRICT:   Верификация обязательна, ошибка при отсутствии/несовпадении.
    WARN:     Верификация по возможности, warning при проблемах.
    DISABLED: Верификация отключена, hash.json игнорируется.
    """

    STRICT = "strict"
    WARN = "warn"
    DISABLED = "disabled"

    @classmethod
    def from_str(cls, value: str) -> VerificationMode:
        """Парсинг строки в VerificationMode.

        Args:
            value: Строка ("strict", "warn", "disabled", регистр не важен).

        Returns:
            Соответствующий VerificationMode.

        Raises:
            ValueError: Если значение не распознано.
        """
        try:
            return cls(value.lower())
        except ValueError:
            raise ValueError(
                f"Неизвестный режим верификации: '{value}'. "
                f"Допустимые значения: strict, warn, disabled"
            ) from None


class VerificationError(Exception):
    """Ошибка верификации модуля."""


@dataclass(frozen=True)
class ModuleManifest:
    """Манифест хешей модуля из hash.json."""

    version: str
    files: dict[str, str]  # путь → hex SHA256
    manifest_hash: str


@dataclass
class VerificationResult:
    """Результат верификации модуля."""

    passed: bool
    module_name: str
    manifest: ModuleManifest | None
    runtime_hashes: dict[str, str]  # путь → hex SHA256
    mismatches: list[str] = field(default_factory=list)
    error: str | None = None


def _canonical_json(files: dict[str, str]) -> str:
    """Каноническое JSON-представление files для вычисления manifest_hash.

    Гарантирует детерминированный порядок ключей и компактный формат.
    """
    return json.dumps(files, sort_keys=True, separators=(",", ":"))


def _compute_hash(data: bytes) -> str:
    """SHA256 hexdigest из байтов."""
    return hashlib.sha256(data).hexdigest()


def load_and_validate_manifest(module_dir: Path) -> ModuleManifest | None:
    """Загрузить и валидировать hash.json модуля.

    Строгая валидация:
    - JSON валиден
    - Структура: {version: str, files: dict[str,str], manifest_hash: str}
    - Все хеши 64 hex символа
    - hash.json не включён в files
    - Нет symlink-файлов в keys
    - Нет path traversal

    Args:
        module_dir: Директория модуля.

    Returns:
        ModuleManifest или None если hash.json нет.

    Raises:
        VerificationError: При любой ошибке валидации.
    """
    hash_file = module_dir / "hash.json"
    if not hash_file.exists():
        return None

    try:
        data = json.loads(hash_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise VerificationError(f"hash.json не является валидным JSON: {e}") from e

    # Проверка структуры
    if not isinstance(data, dict):
        raise VerificationError("hash.json: ожидался объект верхнего уровня")

    for key in ("version", "files", "manifest_hash"):
        if key not in data:
            raise VerificationError(f"hash.json: отсутствует ключ '{key}'")

    version = data["version"]
    files = data["files"]
    manifest_hash = data["manifest_hash"]

    if not isinstance(version, str):
        raise VerificationError("hash.json: 'version' должен быть строкой")
    if not isinstance(files, dict):
        raise VerificationError("hash.json: 'files' должен быть объектом")
    if not isinstance(manifest_hash, str):
        raise VerificationError("hash.json: 'manifest_hash' должен быть строкой")

    # hash.json не должен быть в files
    if "hash.json" in files:
        raise VerificationError("hash.json: 'hash.json' не должен быть в списке файлов")

    # Проверка хешей и отсутствия path traversal
    for name, hex_hash in files.items():
        if not isinstance(hex_hash, str) or len(hex_hash) != 64:
            raise VerificationError(
                f"hash.json: невалидный хеш для '{name}' "
                f"(ожидалось 64 hex символа, получено {len(hex_hash) if isinstance(hex_hash, str) else type(hex_hash)})"
            )
        try:
            int(hex_hash, 16)
        except ValueError:
            raise VerificationError(f"hash.json: хеш '{name}' не является hex")

        # Проверка path traversal: resolved path не должен покидать module_dir
        resolved = (module_dir / name).resolve()
        if not resolved.is_relative_to(module_dir.resolve()):
            raise VerificationError(f"hash.json: path traversal в имени '{name}'")

    return ModuleManifest(version=version, files=files, manifest_hash=manifest_hash)


def compute_runtime_hashes(module_dir: Path) -> dict[str, str]:
    """Вычислить хеши файлов модуля для верификации.

    Включаем: *.py, pyproject.toml, requirements*.txt
    Исключаем: __pycache__/, *.md, hash.json, .git/, .gitignore
    Симлинки пропускаем с warning. Пути относительные POSIX.

    Args:
        module_dir: Директория модуля.

    Returns:
        Словарь {относительный_путь: hex SHA256}.
    """
    include_extensions = {".py", ".toml", ".txt"}
    include_names = {"pyproject.toml"}
    exclude_dirs = {"__pycache__", ".git"}
    exclude_files = {"hash.json", ".gitignore"}

    hashes: dict[str, str] = {}

    for file_path in sorted(module_dir.rglob("*")):
        # Пропуск директорий
        if not file_path.is_file():
            continue

        # Относительный путь
        try:
            rel_path = file_path.relative_to(module_dir)
        except ValueError:
            continue

        rel_posix = rel_path.as_posix()

        # Пропуск исключённых директорий
        parts = rel_path.parts
        if any(part in exclude_dirs for part in parts):
            continue

        # Пропуск исключённых файлов
        if file_path.name in exclude_files:
            continue

        # Фильтр по расширению/имени
        if file_path.name not in include_names and file_path.suffix not in include_extensions:
            continue

        # Симлинки — пропуск с warning
        if file_path.is_symlink():
            log.warning("Пропуск symlink при вычислении хешей: %s", rel_posix)
            continue

        try:
            file_bytes = file_path.read_bytes()
            hashes[rel_posix] = _compute_hash(file_bytes)
        except OSError as e:
            log.warning("Не удалось прочитать файл для хеширования: %s (%s)", rel_posix, e)

    return hashes


def verify_module(module_dir: Path, manifest: ModuleManifest) -> VerificationResult:
    """Сверить runtime-хеши с манифестом.

    Проверяет:
    - runtime_hashes совпадают с manifest.files
    - manifest_hash = sha256(canonical_json(files))

    Args:
        module_dir: Директория модуля.
        manifest: Манифест для сверки.

    Returns:
        VerificationResult с результатом проверки.
    """
    module_name = module_dir.name
    runtime_hashes = compute_runtime_hashes(module_dir)
    mismatches: list[str] = []

    # Сверка файлов
    all_files = set(runtime_hashes.keys()) | set(manifest.files.keys())
    for file_name in sorted(all_files):
        runtime_hash = runtime_hashes.get(file_name)
        manifest_hash = manifest.files.get(file_name)

        if runtime_hash is None:
            mismatches.append(f"Файл '{file_name}' есть в манифесте, но отсутствует на диске")
        elif manifest_hash is None:
            mismatches.append(f"Файл '{file_name}' есть на диске, но отсутствует в манифесте")
        elif runtime_hash != manifest_hash:
            mismatches.append(f"Хеш '{file_name}' не совпадает: runtime={runtime_hash[:16]}... manifest={manifest_hash[:16]}...")

    # Проверка manifest_hash
    expected_manifest_hash = _compute_hash(_canonical_json(manifest.files).encode("utf-8"))
    if expected_manifest_hash != manifest.manifest_hash:
        mismatches.append(
            f"manifest_hash не совпадает: expected={expected_manifest_hash[:16]}... "
            f"got={manifest.manifest_hash[:16]}..."
        )

    passed = len(mismatches) == 0
    return VerificationResult(
        passed=passed,
        module_name=module_name,
        manifest=manifest,
        runtime_hashes=runtime_hashes,
        mismatches=mismatches,
    )


def compare_versions(
    module_name: str,
    manifest_version: str,
    runtime_version: str,
    sha256_passed: bool,
) -> str | None:
    """Сравнить версию из манифеста с рантайм-версией модуля.

    Логика:
    - Если версии совпадают и SHA256 прошёл → OK (None).
    - Если версии совпадают, но SHA256 не совпал → «код изменён без бампа версии».
    - Если версии не совпадают → «код обновлён до X, манифест для Y».

    Args:
        module_name: Имя модуля (для сообщений).
        manifest_version: Версия из hash.json.
        runtime_version: Версия из instance.version (рантайм).
        sha256_passed: Прошла ли SHA256-верификация файлов.

    Returns:
        Текст подсказки (hint) или None если всё ОК.
    """
    if manifest_version == runtime_version:
        if not sha256_passed:
            return (
                f"Модуль '{module_name}': код изменён БЕЗ бампа версии (v{runtime_version}) "
                f"— перегенерируй hash.json; если изменения не ваши — возможна подмена"
            )
        return None

    return (
        f"Модуль '{module_name}': код обновлён до v{runtime_version}, "
        f"манифест для v{manifest_version} "
        f"— перегенерируй: python scripts/generate_hash.py {module_name}"
    )
