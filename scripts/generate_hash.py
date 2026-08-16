#!/usr/bin/env python3
"""CLI для генерации hash.json модулей.

Использование:
    python scripts/generate_hash.py <module_name> [--modules-dir modules/]
    python scripts/generate_hash.py --all [--modules-dir modules/]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Добавляем корень проекта в sys.path для импорта модулей
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from modules_system.verification import _canonical_json, _compute_hash, compute_runtime_hashes


_VERSION_VAR_NAMES = ("__version__", "MODULE_VERSION")


def _extract_version(module_dir: Path) -> str:
    """Извлечь версию из __init__.py (__version__ или MODULE_VERSION).

    Поддерживает оба формата:
        __version__ = "x.y.z"
        MODULE_VERSION = "x.y.z"

    Returns:
        Строка версии или "0.0.0" если не найдено.
    """
    init_file = module_dir / "__init__.py"
    if not init_file.exists():
        return "0.0.0"

    try:
        content = init_file.read_text(encoding="utf-8")
        for line in content.splitlines():
            stripped = line.strip()
            for var_name in _VERSION_VAR_NAMES:
                if stripped.startswith(var_name):
                    parts = stripped.split("=", 1)
                    if len(parts) == 2:
                        return parts[1].strip().strip("\"'")
    except OSError:
        pass

    return "0.0.0"


def _check_no_symlinks(module_dir: Path) -> None:
    """Проверить что в директории модуля нет symlink-файлов/директорий.

    Вызывается ДО compute_runtime_hashes, чтобы поймать symlink на уровне ФС.

    Args:
        module_dir: Директория модуля.

    Raises:
        ValueError: Если обнаружен symlink.
    """
    for item in module_dir.rglob("*"):
        if item.is_symlink():
            try:
                rel = item.relative_to(module_dir)
            except ValueError:
                rel = item
            raise ValueError(f"Обнаружен symlink в модуле: {rel}")


def generate_hash(module_dir: Path) -> None:
    """Сгенерировать hash.json для одного модуля.

    Args:
        module_dir: Директория модуля.

    Raises:
        ValueError: Если обнаружен symlink в модуле.
    """
    # Проверка на symlink самой директории
    if module_dir.is_symlink():
        raise ValueError(f"Модуль является symlink: {module_dir}")

    # Проверка что директория существует
    if not module_dir.is_dir():
        raise FileNotFoundError(f"Директория модуля не найдена: {module_dir}")

    # Честная проверка symlink на уровне ФС (до compute_runtime_hashes)
    _check_no_symlinks(module_dir)

    version = _extract_version(module_dir)
    files = compute_runtime_hashes(module_dir)
    manifest_hash = _compute_hash(_canonical_json(files).encode("utf-8"))

    manifest = {
        "version": version,
        "files": files,
        "manifest_hash": manifest_hash,
    }

    hash_file = module_dir / "hash.json"
    hash_file.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"[OK] {module_dir.name}: {len(files)} файлов, version={version}")


def main() -> None:
    """Точка входа CLI."""
    parser = argparse.ArgumentParser(description="Генерация hash.json для модулей Mia")
    parser.add_argument("modules", nargs="*", help="Имена модулей для генерации")
    parser.add_argument("--all", action="store_true", help="Генерировать для всех модулей")
    parser.add_argument("--modules-dir", default="modules", help="Путь к директории модулей")
    args = parser.parse_args()

    modules_dir = Path(args.modules_dir)
    if not modules_dir.is_absolute():
        modules_dir = _PROJECT_ROOT / modules_dir

    if not modules_dir.is_dir():
        print(f"[ERROR] Директория модулей не найдена: {modules_dir}", file=sys.stderr)
        sys.exit(1)

    if args.all:
        # Найти все модули
        module_dirs = sorted(
            d for d in modules_dir.iterdir()
            if d.is_dir() and (d / "__init__.py").exists()
        )
    elif args.modules:
        module_dirs = [modules_dir / name for name in args.modules]
    else:
        parser.print_help()
        sys.exit(1)

    errors: list[str] = []
    for module_dir in module_dirs:
        try:
            generate_hash(module_dir)
        except (ValueError, FileNotFoundError, OSError) as e:
            errors.append(f"{module_dir.name}: {e}")
            print(f"[FAIL] {module_dir.name}: {e}", file=sys.stderr)

    if errors:
        print(f"\nОшибок: {len(errors)}", file=sys.stderr)
        sys.exit(1)
    else:
        print(f"\nГотово: {len(module_dirs)} модулей")


if __name__ == "__main__":
    main()
