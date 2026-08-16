"""Интеграционные тесты хеш-верификации в ModuleManager.

Проверяет:
- Поведение при разных verification_mode (STRICT, WARN, DISABLED)
- Защиту от symlink и path traversal
- Доступность _verification_metadata на загруженном модуле
- Генерацию hash.json и его валидность
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from modules_system.module_manager import ModuleManager
from modules_system.module_registry import ModuleRegistry
from modules_system.verification import (
    VerificationError,
    VerificationMode,
    _canonical_json,
    _compute_hash,
    load_and_validate_manifest,
    compute_runtime_hashes,
)


# ── Вспомогательные функции ────────────────────────────────────────


def _make_hash(data: str) -> str:
    """SHA256 hexdigest из строки."""
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _generate_manifest(module_dir: Path, version: str = "1.0.0") -> dict:
    """Сгенерировать манифест для директории модуля (как generate_hash.py)."""
    files = compute_runtime_hashes(module_dir)
    manifest_hash = _compute_hash(_canonical_json(files).encode("utf-8"))
    return {"version": version, "files": files, "manifest_hash": manifest_hash}


def _write_hash_json(module_dir: Path, manifest: dict) -> None:
    """Записать hash.json."""
    (module_dir / "hash.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )


def _create_test_module(
    module_dir: Path,
    *,
    name: str = "testmod",
    version: str = "1.0.0",
    content: str | None = None,
    write_hash: bool = True,
    files: dict[str, str] | None = None,
) -> None:
    """Создать минимальный тестовый модуль.

    Args:
        module_dir: Родительская директория (modules_dir).
        name: Имя модуля (поддиректория).
        version: Версия модуля.
        content: Содержимое __init__.py. Если None — автогенерация.
        write_hash: Создать ли hash.json.
        files: Дополнительные файлы {имя: содержимое}.
    """
    mod_dir = module_dir / name
    mod_dir.mkdir(parents=True, exist_ok=True)

    if content is None:
        content = (
            'from modules_system.module_base import ModuleBase\n\n'
            f'class TestModule(ModuleBase):\n'
            f'    @property\n'
            f'    def name(self) -> str:\n'
            f'        return "{name}"\n'
            f'    @property\n'
            f'    def version(self) -> str:\n'
            f'        return "{version}"\n'
        )

    (mod_dir / "__init__.py").write_text(content, encoding="utf-8")

    if files:
        for fname, fcontent in files.items():
            fpath = mod_dir / fname
            fpath.parent.mkdir(parents=True, exist_ok=True)
            fpath.write_text(fcontent, encoding="utf-8")

    if write_hash:
        manifest = _generate_manifest(mod_dir, version=version)
        _write_hash_json(mod_dir, manifest)


# ── Тесты ModuleManager: verification_mode ─────────────────────────


class TestModuleManagerVerification:
    """Интеграционные тесты верификации через ModuleManager."""

    def test_strict_mode_loads_valid_module(self, tmp_path: Path):
        """STRICT: валидный модуль с hash.json загружается."""
        _create_test_module(tmp_path, name="valid_mod")
        mgr = ModuleManager(str(tmp_path), verification_mode=VerificationMode.STRICT)

        instance = mgr.load("valid_mod")

        assert instance is not None
        assert instance.name == "valid_mod"
        assert instance.version == "1.0.0"

    def test_strict_mode_rejects_no_hash(self, tmp_path: Path):
        """STRICT: модуль без hash.json → VerificationError."""
        _create_test_module(tmp_path, name="no_hash", write_hash=False)
        mgr = ModuleManager(str(tmp_path), verification_mode=VerificationMode.STRICT)

        with pytest.raises(VerificationError, match="hash.json отсутствует"):
            mgr.load("no_hash")

    def test_warn_mode_allows_no_hash(self, tmp_path: Path):
        """WARN: модуль без hash.json загружается (с warning)."""
        _create_test_module(tmp_path, name="no_hash", write_hash=False)
        mgr = ModuleManager(str(tmp_path), verification_mode=VerificationMode.WARN)

        instance = mgr.load("no_hash")

        assert instance is not None
        assert instance.name == "no_hash"

    def test_disabled_mode_skips_verification(self, tmp_path: Path):
        """DISABLED: hash.json не проверяется."""
        _create_test_module(tmp_path, name="dis_mod")
        mgr = ModuleManager(str(tmp_path), verification_mode=VerificationMode.DISABLED)

        instance = mgr.load("dis_mod")

        assert instance is not None

    def test_disabled_mode_no_hash_loads(self, tmp_path: Path):
        """DISABLED: модуль без hash.json тоже загружается."""
        _create_test_module(tmp_path, name="no_hash", write_hash=False)
        mgr = ModuleManager(str(tmp_path), verification_mode=VerificationMode.DISABLED)

        instance = mgr.load("no_hash")

        assert instance is not None

    def test_strict_mode_rejects_tampered_hash(self, tmp_path: Path):
        """STRICT: подменённый файл → VerificationError."""
        _create_test_module(tmp_path, name="tampered")
        # Подменяем содержимое файла
        init_file = tmp_path / "tampered" / "__init__.py"
        init_file.write_text("# TAMPERED!", encoding="utf-8")
        mgr = ModuleManager(str(tmp_path), verification_mode=VerificationMode.STRICT)

        with pytest.raises(VerificationError, match="верификация не прошла"):
            mgr.load("tampered")

    def test_warn_mode_loads_tampered_hash(self, tmp_path: Path):
        """WARN: подменённый файл загружается (с warning).

        Подменяем __init__.py на другой валидный модуль (с ModuleBase),
        но с другим содержимым — хеш не совпадёт.
        """
        _create_test_module(tmp_path, name="tampered")
        # Подменяем на валидный модуль с другим содержимым
        init_file = tmp_path / "tampered" / "__init__.py"
        tampered_content = (
            'from modules_system.module_base import ModuleBase\n\n'
            'class TestModule(ModuleBase):\n'
            '    @property\n'
            '    def name(self) -> str:\n'
            '        return "tampered"\n'
            '    @property\n'
            '    def version(self) -> str:\n'
            '        return "9.9.9"\n'
        )
        init_file.write_text(tampered_content, encoding="utf-8")
        mgr = ModuleManager(str(tmp_path), verification_mode=VerificationMode.WARN)

        instance = mgr.load("tampered")

        assert instance is not None

    def test_warn_mode_rejects_invalid_manifest(self, tmp_path: Path):
        """WARN: невалидный hash.json загружается (с warning, не ошибка)."""
        _create_test_module(tmp_path, name="bad_hash")
        # Перезаписываем hash.json невалидным содержимым
        (tmp_path / "bad_hash" / "hash.json").write_text("{bad}", encoding="utf-8")
        mgr = ModuleManager(str(tmp_path), verification_mode=VerificationMode.WARN)

        instance = mgr.load("bad_hash")

        assert instance is not None

    def test_strict_mode_rejects_invalid_manifest(self, tmp_path: Path):
        """STRICT: невалидный hash.json → VerificationError."""
        _create_test_module(tmp_path, name="bad_hash")
        (tmp_path / "bad_hash" / "hash.json").write_text("{bad}", encoding="utf-8")
        mgr = ModuleManager(str(tmp_path), verification_mode=VerificationMode.STRICT)

        with pytest.raises(VerificationError, match="не является валидным JSON"):
            mgr.load("bad_hash")

    def test_symlink_module_rejected(self, tmp_path: Path):
        """Symlink-директория модуля → ValueError."""
        real_dir = tmp_path / "real_module"
        _create_test_module(tmp_path, name="real_module")
        symlink_dir = tmp_path / "link_module"
        symlink_dir.symlink_to(real_dir)
        mgr = ModuleManager(str(tmp_path), verification_mode=VerificationMode.STRICT)

        with pytest.raises(ValueError, match="symlink"):
            mgr.load("link_module")

    def test_path_traversal_in_name_rejected(self, tmp_path: Path):
        """Path traversal в имени модуля → ValueError."""
        # Создаём файл «за пределами» modules_dir
        outside = tmp_path / "outside.py"
        outside.write_text("# evil", encoding="utf-8")
        mgr = ModuleManager(str(tmp_path), verification_mode=VerificationMode.STRICT)

        with pytest.raises(ValueError):
            mgr.load("../outside")

    def test_metadata_set_on_loaded_module(self, tmp_path: Path):
        """_verification_metadata установлен на экземпляре модуля после загрузки.

        Поля: manifest_hash, version, verified, mode, version_code, version_manifest, hint.
        """
        _create_test_module(tmp_path, name="meta_mod")
        mgr = ModuleManager(str(tmp_path), verification_mode=VerificationMode.STRICT)

        instance = mgr.load("meta_mod")

        assert hasattr(instance, "_verification_metadata")
        meta = instance._verification_metadata
        assert meta["verified"] is True
        assert meta["version"] == "1.0.0"
        assert isinstance(meta["manifest_hash"], str)
        assert len(meta["manifest_hash"]) == 64
        assert meta["mode"] == "strict"
        # Новые поля
        assert meta["version_code"] == "1.0.0"
        assert meta["version_manifest"] == "1.0.0"
        assert meta["hint"] is None  # версии совпадают, SHA256 ОК

    def test_metadata_set_when_disabled(self, tmp_path: Path):
        """_verification_metadata установлен при DISABLED режиме (verified=False)."""
        _create_test_module(tmp_path, name="dis_mod")
        mgr = ModuleManager(str(tmp_path), verification_mode=VerificationMode.DISABLED)

        instance = mgr.load("dis_mod")

        assert hasattr(instance, "_verification_metadata")
        meta = instance._verification_metadata
        assert meta["verified"] is False
        assert meta["manifest_hash"] is None
        assert meta["version"] is None
        assert meta["mode"] == "disabled"
        # Новые поля
        assert meta["version_code"] == "1.0.0"
        assert meta["version_manifest"] is None
        assert meta["hint"] is None

    def test_module_with_subdirectory(self, tmp_path: Path):
        """Модуль с поддиректориями корректно верифицируется."""
        _create_test_module(
            tmp_path,
            name="submod",
            files={"sub/helper.py": "# helper"},
        )
        mgr = ModuleManager(str(tmp_path), verification_mode=VerificationMode.STRICT)

        instance = mgr.load("submod")

        assert instance is not None

    def test_module_with_requirements(self, tmp_path: Path):
        """Модуль с requirements.txt верифицируется."""
        _create_test_module(
            tmp_path,
            name="reqmod",
            files={"requirements.txt": "pytest>=7.0\n"},
        )
        mgr = ModuleManager(str(tmp_path), verification_mode=VerificationMode.STRICT)

        instance = mgr.load("reqmod")

        assert instance is not None

    def test_default_verification_mode_is_strict(self, tmp_path: Path):
        """По умолчанию verification_mode = STRICT."""
        mgr = ModuleManager(str(tmp_path))
        assert mgr.verification_mode == VerificationMode.STRICT

    def test_strict_mode_rejects_version_mismatch(self, tmp_path: Path):
        """STRICT: код обновлён до v2.0.0, манифест для v1.0.0 → VerificationError.

        В STRICT режиме SHA256-ошибка перехватывается раньше, чем сверка версий,
        поэтому ошибка содержит инфо о несовпадении хешей.
        """
        _create_test_module(tmp_path, name="ver_mod", version="1.0.0")
        init_file = tmp_path / "ver_mod" / "__init__.py"
        tampered_content = (
            'from modules_system.module_base import ModuleBase\n\n'
            'class TestModule(ModuleBase):\n'
            '    @property\n'
            '    def name(self) -> str:\n'
            '        return "ver_mod"\n'
            '    @property\n'
            '    def version(self) -> str:\n'
            '        return "2.0.0"\n'
        )
        init_file.write_text(tampered_content, encoding="utf-8")
        mgr = ModuleManager(str(tmp_path), verification_mode=VerificationMode.STRICT)

        with pytest.raises(VerificationError):
            mgr.load("ver_mod")

    def test_warn_mode_loads_version_mismatch(self, tmp_path: Path):
        """WARN: код обновлён до v2.0.0, манифест для v1.0.0 → загружается с warning."""
        _create_test_module(tmp_path, name="ver_mod", version="1.0.0")
        init_file = tmp_path / "ver_mod" / "__init__.py"
        tampered_content = (
            'from modules_system.module_base import ModuleBase\n\n'
            'class TestModule(ModuleBase):\n'
            '    @property\n'
            '    def name(self) -> str:\n'
            '        return "ver_mod"\n'
            '    @property\n'
            '    def version(self) -> str:\n'
            '        return "2.0.0"\n'
        )
        init_file.write_text(tampered_content, encoding="utf-8")
        mgr = ModuleManager(str(tmp_path), verification_mode=VerificationMode.WARN)

        instance = mgr.load("ver_mod")

        assert instance is not None
        meta = instance._verification_metadata
        assert meta["verified"] is False
        assert meta["hint"] is not None
        assert "код обновлён до v2.0.0" in meta["hint"]
        assert "манифест для v1.0.0" in meta["hint"]

    def test_warn_mode_hint_no_version_bump(self, tmp_path: Path):
        """WARN: код изменён без бампа версии → hint «без бампа версии»."""
        _create_test_module(tmp_path, name="nobump_mod", version="1.0.0")
        # Подменяем содержимое файла, но оставляем ту же версию
        init_file = tmp_path / "nobump_mod" / "__init__.py"
        tampered_content = (
            'from modules_system.module_base import ModuleBase\n\n'
            'class TestModule(ModuleBase):\n'
            '    @property\n'
            '    def name(self) -> str:\n'
            '        return "nobump_mod"\n'
            '    @property\n'
            '    def version(self) -> str:\n'
            '        return "1.0.0"\n'
            '    # Тут другое содержимое\n'
            '    def extra(self): pass\n'
        )
        init_file.write_text(tampered_content, encoding="utf-8")
        mgr = ModuleManager(str(tmp_path), verification_mode=VerificationMode.WARN)

        instance = mgr.load("nobump_mod")

        assert instance is not None
        meta = instance._verification_metadata
        assert meta["verified"] is False
        assert meta["hint"] is not None
        assert "БЕЗ бампа версии" in meta["hint"]
        assert "v1.0.0" in meta["hint"]

    def test_strict_version_mismatch_only_version_differs(self, tmp_path: Path):
        """STRICT: SHA256 ОК, но манифест для старой версии → VerificationError с hint.

        Сценарий: hash.json содержит version=0.9.0, файлы совпадают (SHA256 OK),
        но runtime instance.version = 1.0.0.
        """
        _create_test_module(tmp_path, name="ver_only", version="1.0.0")
        # Меняем версию в hash.json на старую, но файлы оставляем теми же
        import json as _json
        hash_file = tmp_path / "ver_only" / "hash.json"
        data = _json.loads(hash_file.read_text(encoding="utf-8"))
        data["version"] = "0.9.0"
        # Пересчитываем manifest_hash для новой версии (файлы не менялись)
        from modules_system.verification import _canonical_json, _compute_hash
        data["manifest_hash"] = _compute_hash(_canonical_json(data["files"]).encode("utf-8"))
        hash_file.write_text(_json.dumps(data, indent=2), encoding="utf-8")

        mgr = ModuleManager(str(tmp_path), verification_mode=VerificationMode.STRICT)

        with pytest.raises(VerificationError, match="код обновлён до v1.0.0"):
            mgr.load("ver_only")

    def test_strict_version_mismatch_manifest_newer(self, tmp_path: Path):
        """STRICT: SHA256 ОК, манифест для newer версии → VerificationError.

        Сценарий: hash.json version=2.0.0, но runtime version=1.0.0
        (откатили код, но не перегенерировали hash.json).
        """
        _create_test_module(tmp_path, name="ver_old", version="1.0.0")
        import json as _json
        hash_file = tmp_path / "ver_old" / "hash.json"
        data = _json.loads(hash_file.read_text(encoding="utf-8"))
        data["version"] = "2.0.0"
        from modules_system.verification import _canonical_json, _compute_hash
        data["manifest_hash"] = _compute_hash(_canonical_json(data["files"]).encode("utf-8"))
        hash_file.write_text(_json.dumps(data, indent=2), encoding="utf-8")

        mgr = ModuleManager(str(tmp_path), verification_mode=VerificationMode.STRICT)

        with pytest.raises(VerificationError, match="манифест для v2.0.0"):
            mgr.load("ver_old")

    def test_warn_version_mismatch_all_metadata_fields(self, tmp_path: Path):
        """WARN: version mismatch → все поля metadata заполнены корректно."""
        _create_test_module(tmp_path, name="ver_meta", version="1.0.0")
        import json as _json
        hash_file = tmp_path / "ver_meta" / "hash.json"
        data = _json.loads(hash_file.read_text(encoding="utf-8"))
        data["version"] = "0.5.0"
        from modules_system.verification import _canonical_json, _compute_hash
        data["manifest_hash"] = _compute_hash(_canonical_json(data["files"]).encode("utf-8"))
        hash_file.write_text(_json.dumps(data, indent=2), encoding="utf-8")

        mgr = ModuleManager(str(tmp_path), verification_mode=VerificationMode.WARN)

        instance = mgr.load("ver_meta")

        meta = instance._verification_metadata
        assert meta["verified"] is True  # SHA256 passed
        assert meta["version"] == "0.5.0"  # from manifest
        assert meta["version_code"] == "1.0.0"  # runtime
        assert meta["version_manifest"] == "0.5.0"  # from manifest
        assert meta["hint"] is not None
        assert "код обновлён до v1.0.0" in meta["hint"]
        assert "манифест для v0.5.0" in meta["hint"]
        assert meta["mode"] == "warn"


# ── Тесты ModuleRegistry: verification_mode ────────────────────────


class TestModuleRegistryVerification:
    """Интеграционные тесты верификации через ModuleRegistry."""

    def test_registry_strict_mode_rejects_no_hash(self, tmp_path: Path):
        """Registry STRICT: модуль без hash.json → VerificationError."""
        _create_test_module(tmp_path, name="no_hash", write_hash=False)
        registry = ModuleRegistry(
            str(tmp_path), verification_mode=VerificationMode.STRICT
        )

        with pytest.raises(VerificationError):
            registry.load("no_hash")

    def test_registry_warn_mode_allows_no_hash(self, tmp_path: Path):
        """Registry WARN: модуль без hash.json загружается."""
        _create_test_module(tmp_path, name="no_hash", write_hash=False)
        registry = ModuleRegistry(
            str(tmp_path), verification_mode=VerificationMode.WARN
        )

        instance = registry.load("no_hash")

        assert instance is not None

    def test_registry_disabled_mode_skips(self, tmp_path: Path):
        """Registry DISABLED: верификация отключена."""
        _create_test_module(tmp_path, name="dis_mod")
        registry = ModuleRegistry(
            str(tmp_path), verification_mode=VerificationMode.DISABLED
        )

        instance = registry.load("dis_mod")

        assert instance is not None

    def test_registry_verification_mode_property(self, tmp_path: Path):
        """Registry.verification_mode возвращает текущий режим."""
        registry = ModuleRegistry(
            str(tmp_path), verification_mode=VerificationMode.WARN
        )
        assert registry.verification_mode == VerificationMode.WARN


# ── Тесты generate_hash.py (интеграция) ────────────────────────────


class TestGenerateHashIntegration:
    """Интеграционные тесты генерации hash.json."""

    def test_generate_and_verify_roundtrip(self, tmp_path: Path):
        """Генерация hash.json → load_and_validate → verify — всё проходит."""
        _create_test_module(tmp_path, name="roundtrip")
        module_dir = tmp_path / "roundtrip"

        # Генерируем манифест
        manifest = _generate_manifest(module_dir)
        _write_hash_json(module_dir, manifest)

        # Загружаем и валидируем
        loaded = load_and_validate_manifest(module_dir)
        assert loaded is not None

        # Верифицируем
        from modules_system.verification import verify_module
        result = verify_module(module_dir, loaded)
        assert result.passed is True

    def test_tampered_file_fails_roundtrip(self, tmp_path: Path):
        """Генерация → подмена файла → verify падает."""
        _create_test_module(tmp_path, name="tamper_test")
        module_dir = tmp_path / "tamper_test"

        manifest = _generate_manifest(module_dir)
        _write_hash_json(module_dir, manifest)

        # Подменяем файл
        (module_dir / "__init__.py").write_text("# TAMPERED", encoding="utf-8")

        loaded = load_and_validate_manifest(module_dir)
        assert loaded is not None

        from modules_system.verification import verify_module
        result = verify_module(module_dir, loaded)
        assert result.passed is False


# ── Тесты Application: реестры верификации ─────────────────────────


class TestApplicationVerification:
    """Интеграционные тесты: Application заполняет module_versions и module_verification."""

    def test_module_versions_populated(self, tmp_path: Path):
        """Application.module_versions заполняется после загрузки модуля."""
        from core.application import Application

        _create_test_module(tmp_path, name="app_mod")
        # Копируем module_base в tmp_path для импорта
        _copy_module_base(tmp_path)

        app = Application(modules_dir=str(tmp_path), verification_mode=VerificationMode.STRICT)
        app.load_module("app_mod")

        assert "app_mod" in app.module_versions
        version_entry = app.module_versions["app_mod"]
        # Формат: "version:manifest_hash"
        parts = version_entry.split(":")
        assert len(parts) == 2
        assert parts[0] == "1.0.0"
        assert len(parts[1]) == 64  # SHA256 hex

    def test_module_verification_populated(self, tmp_path: Path):
        """Application.module_verification заполняется после загрузки модуля."""
        from core.application import Application

        _create_test_module(tmp_path, name="app_mod")
        _copy_module_base(tmp_path)

        app = Application(modules_dir=str(tmp_path), verification_mode=VerificationMode.STRICT)
        app.load_module("app_mod")

        assert "app_mod" in app.module_verification
        assert app.module_verification["app_mod"] is True  # SHA256 passed

    def test_module_verification_disabled(self, tmp_path: Path):
        """DISABLED: module_verification содержит False."""
        from core.application import Application

        _create_test_module(tmp_path, name="app_mod")
        _copy_module_base(tmp_path)

        app = Application(modules_dir=str(tmp_path), verification_mode=VerificationMode.DISABLED)
        app.load_module("app_mod")

        assert "app_mod" in app.module_verification
        assert app.module_verification["app_mod"] is False

    def test_module_versions_cleared_on_unload(self, tmp_path: Path):
        """При выгрузке модуль НЕ удаляется из module_versions (реестр аудита)."""
        from core.application import Application

        _create_test_module(tmp_path, name="app_mod")
        _copy_module_base(tmp_path)

        app = Application(modules_dir=str(tmp_path), verification_mode=VerificationMode.STRICT)
        app.load_module("app_mod")
        assert "app_mod" in app.module_versions

        app.unload_module("app_mod")
        # Реестр верификации — аудит, модуль НЕ удаляется
        assert "app_mod" in app.module_versions

    def test_module_versions_multiple_modules(self, tmp_path: Path):
        """module_versions для нескольких модулей."""
        from core.application import Application

        _create_test_module(tmp_path, name="mod_a", version="1.0.0")
        _create_test_module(tmp_path, name="mod_b", version="2.0.0")
        _copy_module_base(tmp_path)

        app = Application(modules_dir=str(tmp_path), verification_mode=VerificationMode.STRICT)
        app.load_module("mod_a")
        app.load_module("mod_b")

        assert "mod_a" in app.module_versions
        assert "mod_b" in app.module_versions
        assert app.module_versions["mod_a"].startswith("1.0.0:")
        assert app.module_versions["mod_b"].startswith("2.0.0:")


def _copy_module_base(tmp_path: Path) -> None:
    """Скопировать modules_system/module_base.py в tmp_path для импорта."""
    import shutil
    src_dir = Path(__file__).resolve().parent.parent / "modules_system"
    dst_dir = tmp_path / "modules_system"
    if not dst_dir.exists():
        dst_dir.mkdir()
    src = src_dir / "module_base.py"
    dst = dst_dir / "module_base.py"
    if src.exists() and not dst.exists():
        shutil.copy2(src, dst)
    # Также копируем __init__.py если его нет
    init = dst_dir / "__init__.py"
    if not init.exists():
        init.write_text("", encoding="utf-8")
