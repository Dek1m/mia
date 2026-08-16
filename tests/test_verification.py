"""Юнит-тесты для модуля верификации модулей (verification.py).

Покрывает:
- load_and_validate_manifest: валидные/невалидные манифесты, edge cases
- compute_runtime_hashes: включение/исключение файлов, symlink
- verify_module: сверка хешей, manifest_hash, краевые случаи
"""
from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from modules_system.verification import (
    VerificationError,
    VerificationMode,
    ModuleManifest,
    VerificationResult,
    _canonical_json,
    _compute_hash,
    compare_versions,
    load_and_validate_manifest,
    compute_runtime_hashes,
    verify_module,
)


# ── Вспомогательные функции ────────────────────────────────────────


def _make_hash(data: str) -> str:
    """SHA256 hexdigest из строки."""
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _make_manifest(
    version: str = "1.0.0",
    files: dict[str, str] | None = None,
    *,
    compute_manifest_hash: bool = True,
) -> dict:
    """Создать словарь манифеста с опциональным вычислением manifest_hash."""
    if files is None:
        files = {}
    manifest = {"version": version, "files": files}
    if compute_manifest_hash:
        manifest["manifest_hash"] = _compute_hash(
            _canonical_json(files).encode("utf-8")
        )
    return manifest


def _write_hash_json(module_dir: Path, manifest: dict) -> None:
    """Записать hash.json в директорию модуля."""
    hash_file = module_dir / "hash.json"
    hash_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _create_module_file(module_dir: Path, name: str, content: str = "# test") -> None:
    """Создать файл в директории модуля и вернуть его хеш."""
    file_path = module_dir / name
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")
    return _compute_hash(content.encode("utf-8"))


# ── Тесты load_and_validate_manifest ────────────────────────────────


class TestLoadAndValidateManifest:
    """Тесты загрузки и валидации hash.json."""

    def test_load_valid_manifest(self, tmp_path: Path):
        """Валидный hash.json возвращает ModuleManifest."""
        # Arrange
        module_dir = tmp_path / "test_module"
        module_dir.mkdir()
        h = _make_hash("content")
        manifest = _make_manifest(files={"__init__.py": h})
        _write_hash_json(module_dir, manifest)

        # Act
        result = load_and_validate_manifest(module_dir)

        # Assert
        assert result is not None
        assert isinstance(result, ModuleManifest)
        assert result.version == "1.0.0"
        assert result.files == {"__init__.py": h}
        assert result.manifest_hash == manifest["manifest_hash"]

    def test_load_missing_manifest_returns_none(self, tmp_path: Path):
        """Отсутствие hash.json → None (не ошибка)."""
        module_dir = tmp_path / "test_module"
        module_dir.mkdir()

        result = load_and_validate_manifest(module_dir)

        assert result is None

    def test_load_invalid_json_raises_error(self, tmp_path: Path):
        """Невалидный JSON → VerificationError."""
        module_dir = tmp_path / "test_module"
        module_dir.mkdir()
        (module_dir / "hash.json").write_text("{invalid json!!!", encoding="utf-8")

        with pytest.raises(VerificationError, match="не является валидным JSON"):
            load_and_validate_manifest(module_dir)

    def test_load_invalid_structure_not_dict(self, tmp_path: Path):
        """JSON-массив вместо объекта → VerificationError."""
        module_dir = tmp_path / "test_module"
        module_dir.mkdir()
        (module_dir / "hash.json").write_text("[1, 2, 3]", encoding="utf-8")

        with pytest.raises(VerificationError, match="объект верхнего уровня"):
            load_and_validate_manifest(module_dir)

    def test_load_invalid_structure_missing_version(self, tmp_path: Path):
        """Отсутствие ключа 'version' → VerificationError."""
        module_dir = tmp_path / "test_module"
        module_dir.mkdir()
        data = {"files": {}, "manifest_hash": "abc"}
        _write_hash_json(module_dir, data)

        with pytest.raises(VerificationError, match="version"):
            load_and_validate_manifest(module_dir)

    def test_load_invalid_structure_missing_files(self, tmp_path: Path):
        """Отсутствие ключа 'files' → VerificationError."""
        module_dir = tmp_path / "test_module"
        module_dir.mkdir()
        data = {"version": "1.0.0", "manifest_hash": "abc"}
        _write_hash_json(module_dir, data)

        with pytest.raises(VerificationError, match="files"):
            load_and_validate_manifest(module_dir)

    def test_load_invalid_structure_missing_manifest_hash(self, tmp_path: Path):
        """Отсутствие ключа 'manifest_hash' → VerificationError."""
        module_dir = tmp_path / "test_module"
        module_dir.mkdir()
        data = {"version": "1.0.0", "files": {}}
        _write_hash_json(module_dir, data)

        with pytest.raises(VerificationError, match="manifest_hash"):
            load_and_validate_manifest(module_dir)

    def test_load_invalid_version_not_string(self, tmp_path: Path):
        """version не строка → VerificationError."""
        module_dir = tmp_path / "test_module"
        module_dir.mkdir()
        data = {"version": 123, "files": {}, "manifest_hash": "abc"}
        _write_hash_json(module_dir, data)

        with pytest.raises(VerificationError, match="строкой"):
            load_and_validate_manifest(module_dir)

    def test_load_invalid_files_not_dict(self, tmp_path: Path):
        """files не объект → VerificationError."""
        module_dir = tmp_path / "test_module"
        module_dir.mkdir()
        data = {"version": "1.0.0", "files": "not_a_dict", "manifest_hash": "abc"}
        _write_hash_json(module_dir, data)

        with pytest.raises(VerificationError, match="объектом"):
            load_and_validate_manifest(module_dir)

    def test_load_short_hash_raises_error(self, tmp_path: Path):
        """Хеш короче 64 символов → VerificationError."""
        module_dir = tmp_path / "test_module"
        module_dir.mkdir()
        data = _make_manifest(files={"__init__.py": "abc123"})
        _write_hash_json(module_dir, data)

        with pytest.raises(VerificationError, match="64 hex"):
            load_and_validate_manifest(module_dir)

    def test_load_hash_json_in_files_raises_error(self, tmp_path: Path):
        """hash.json в списке файлов → VerificationError."""
        module_dir = tmp_path / "test_module"
        module_dir.mkdir()
        data = _make_manifest(files={"hash.json": "a" * 64, "__init__.py": "b" * 64})
        _write_hash_json(module_dir, data)

        with pytest.raises(VerificationError, match="hash.json.*не должен быть"):
            load_and_validate_manifest(module_dir)

    def test_load_non_hex_hash_raises_error(self, tmp_path: Path):
        """Хеш не hex → VerificationError."""
        module_dir = tmp_path / "test_module"
        module_dir.mkdir()
        # 64 символа, но не hex (содержит 'g')
        bad_hash = "g" * 64
        data = _make_manifest(files={"__init__.py": bad_hash})
        _write_hash_json(module_dir, data)

        with pytest.raises(VerificationError, match="hex"):
            load_and_validate_manifest(module_dir)

    def test_load_path_traversal_raises_error(self, tmp_path: Path):
        """Path traversal в имени файла → VerificationError."""
        module_dir = tmp_path / "test_module"
        module_dir.mkdir()
        data = _make_manifest(files={"../escape.py": "a" * 64})
        _write_hash_json(module_dir, data)

        with pytest.raises(VerificationError, match="path traversal"):
            load_and_validate_manifest(module_dir)

    def test_load_version_type_not_string(self, tmp_path: Path):
        """version = bool → VerificationError (bool это подкласс int)."""
        module_dir = tmp_path / "test_module"
        module_dir.mkdir()
        data = {"version": True, "files": {}, "manifest_hash": "abc"}
        _write_hash_json(module_dir, data)

        with pytest.raises(VerificationError, match="строкой"):
            load_and_validate_manifest(module_dir)

    def test_load_manifest_hash_not_string(self, tmp_path: Path):
        """manifest_hash не строка → VerificationError."""
        module_dir = tmp_path / "test_module"
        module_dir.mkdir()
        data = {"version": "1.0.0", "files": {}, "manifest_hash": 12345}
        _write_hash_json(module_dir, data)

        with pytest.raises(VerificationError, match="строкой"):
            load_and_validate_manifest(module_dir)


# ── Тесты compute_runtime_hashes ────────────────────────────────────


class TestComputeRuntimeHashes:
    """Тесты вычисления runtime-хешей файлов модуля."""

    def test_includes_python_files(self, tmp_path: Path):
        """*.py файлы включены в хеширование."""
        module_dir = tmp_path / "test_module"
        module_dir.mkdir()
        init_hash = _create_module_file(module_dir, "__init__.py", "# init")
        util_hash = _create_module_file(module_dir, "utils.py", "# utils")

        hashes = compute_runtime_hashes(module_dir)

        assert "__init__.py" in hashes
        assert "utils.py" in hashes
        assert hashes["__init__.py"] == init_hash
        assert hashes["utils.py"] == util_hash

    def test_includes_pyproject_toml(self, tmp_path: Path):
        """pyproject.toml включён в хеширование."""
        module_dir = tmp_path / "test_module"
        module_dir.mkdir()
        toml_hash = _create_module_file(
            module_dir, "pyproject.toml", "[project]\nname = 'test'"
        )

        hashes = compute_runtime_hashes(module_dir)

        assert "pyproject.toml" in hashes
        assert hashes["pyproject.toml"] == toml_hash

    def test_includes_requirements_txt(self, tmp_path: Path):
        """requirements*.txt включены в хеширование."""
        module_dir = tmp_path / "test_module"
        module_dir.mkdir()
        req_hash = _create_module_file(
            module_dir, "requirements.txt", "pytest>=7.0"
        )

        hashes = compute_runtime_hashes(module_dir)

        assert "requirements.txt" in hashes
        assert hashes["requirements.txt"] == req_hash

    def test_includes_requirements_dev_txt(self, tmp_path: Path):
        """requirements-dev.txt включён в хеширование."""
        module_dir = tmp_path / "test_module"
        module_dir.mkdir()
        req_hash = _create_module_file(
            module_dir, "requirements-dev.txt", "black>=23.0"
        )

        hashes = compute_runtime_hashes(module_dir)

        assert "requirements-dev.txt" in hashes

    def test_excludes_pycache(self, tmp_path: Path):
        """__pycache__/ исключён из хеширования."""
        module_dir = tmp_path / "test_module"
        module_dir.mkdir()
        _create_module_file(module_dir, "__init__.py", "# init")
        # Создаём __pycache__ с файлом
        pycache = module_dir / "__pycache__"
        pycache.mkdir()
        (pycache / "module.cpython-310.pyc").write_bytes(b"\x00\x00")

        hashes = compute_runtime_hashes(module_dir)

        assert "__init__.py" in hashes
        assert not any("__pycache__" in k for k in hashes)

    def test_excludes_git_dir(self, tmp_path: Path):
        """*.git/ исключён из хеширования."""
        module_dir = tmp_path / "test_module"
        module_dir.mkdir()
        _create_module_file(module_dir, "__init__.py", "# init")
        git_dir = module_dir / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text("git config")

        hashes = compute_runtime_hashes(module_dir)

        assert "__init__.py" in hashes
        assert not any(".git" in k for k in hashes)

    def test_excludes_md_files(self, tmp_path: Path):
        """*.md файлы исключены из хеширования."""
        module_dir = tmp_path / "test_module"
        module_dir.mkdir()
        _create_module_file(module_dir, "__init__.py", "# init")
        _create_module_file(module_dir, "README.md", "# README")

        hashes = compute_runtime_hashes(module_dir)

        assert "__init__.py" in hashes
        assert "README.md" not in hashes

    def test_excludes_hash_json(self, tmp_path: Path):
        """hash.json исключён из хеширования."""
        module_dir = tmp_path / "test_module"
        module_dir.mkdir()
        _create_module_file(module_dir, "__init__.py", "# init")
        _create_module_file(module_dir, "hash.json", "{}")

        hashes = compute_runtime_hashes(module_dir)

        assert "__init__.py" in hashes
        assert "hash.json" not in hashes

    def test_excludes_gitignore(self, tmp_path: Path):
        """.gitignore исключён из хеширования."""
        module_dir = tmp_path / "test_module"
        module_dir.mkdir()
        _create_module_file(module_dir, "__init__.py", "# init")
        _create_module_file(module_dir, ".gitignore", "*.pyc")

        hashes = compute_runtime_hashes(module_dir)

        assert "__init__.py" in hashes
        assert ".gitignore" not in hashes

    def test_skips_symlink_with_warning(self, tmp_path: Path):
        """Symlink-файлы пропускаются с warning."""
        module_dir = tmp_path / "test_module"
        module_dir.mkdir()
        _create_module_file(module_dir, "__init__.py", "# init")
        # Создаём реальный файл и symlink на него
        real_file = tmp_path / "real_file.py"
        real_file.write_text("# real", encoding="utf-8")
        symlink = module_dir / "linked.py"
        symlink.symlink_to(real_file)

        hashes = compute_runtime_hashes(module_dir)

        assert "__init__.py" in hashes
        assert "linked.py" not in hashes

    def test_relative_posix_paths(self, tmp_path: Path):
        """Пути в результатах — относительные POSIX."""
        module_dir = tmp_path / "test_module"
        module_dir.mkdir()
        sub_dir = module_dir / "sub"
        sub_dir.mkdir()
        _create_module_file(module_dir, "__init__.py", "# init")
        _create_module_file(sub_dir, "helper.py", "# helper")

        hashes = compute_runtime_hashes(module_dir)

        assert "__init__.py" in hashes
        assert "sub/helper.py" in hashes
        # Убедимся что нет абсолютных путей
        for key in hashes:
            assert not Path(key).is_absolute()

    def test_empty_module_dir(self, tmp_path: Path):
        """Пустая директория модуля → пустой словарь."""
        module_dir = tmp_path / "test_module"
        module_dir.mkdir()

        hashes = compute_runtime_hashes(module_dir)

        assert hashes == {}

    def test_sorted_output(self, tmp_path: Path):
        """Файлы отсортированы по имени."""
        module_dir = tmp_path / "test_module"
        module_dir.mkdir()
        _create_module_file(module_dir, "z_last.py", "# z")
        _create_module_file(module_dir, "a_first.py", "# a")
        _create_module_file(module_dir, "__init__.py", "# init")

        hashes = compute_runtime_hashes(module_dir)

        keys = list(hashes.keys())
        assert keys == sorted(keys)

    def test_unreadable_file_skipped(self, tmp_path: Path):
        """Нечитаемый файл пропускается (warning, не ошибка)."""
        module_dir = tmp_path / "test_module"
        module_dir.mkdir()
        _create_module_file(module_dir, "__init__.py", "# init")
        unreadable = module_dir / "unreadable.py"
        unreadable.write_text("# secret", encoding="utf-8")
        unreadable.chmod(0o000)

        try:
            hashes = compute_runtime_hashes(module_dir)
            # __init__.py должен быть, unreadable.py — нет
            assert "__init__.py" in hashes
            # unreadable.py может быть или не быть, зависит от прав
        finally:
            # Восстанавливаем права для очистки tmp_path
            unreadable.chmod(0o644)


# ── Тесты verify_module ────────────────────────────────────────────


class TestVerifyModule:
    """Тесты сверки runtime-хешей с манифестом."""

    def test_verify_passes(self, tmp_path: Path):
        """Верификация проходит: runtime совпадает с манифестом."""
        module_dir = tmp_path / "test_module"
        module_dir.mkdir()
        h = _create_module_file(module_dir, "__init__.py", "# test content")
        manifest = ModuleManifest(
            version="1.0.0",
            files={"__init__.py": h},
            manifest_hash=_compute_hash(
                _canonical_json({"__init__.py": h}).encode("utf-8")
            ),
        )

        result = verify_module(module_dir, manifest)

        assert result.passed is True
        assert result.mismatches == []
        assert result.module_name == "test_module"

    def test_verify_fails_file_hash_mismatch(self, tmp_path: Path):
        """Верификация падает: хеш файла не совпадает."""
        module_dir = tmp_path / "test_module"
        module_dir.mkdir()
        _create_module_file(module_dir, "__init__.py", "# actual content")
        manifest = ModuleManifest(
            version="1.0.0",
            files={"__init__.py": _make_hash("wrong content")},
            manifest_hash="a" * 64,
        )

        result = verify_module(module_dir, manifest)

        assert result.passed is False
        assert len(result.mismatches) >= 1
        assert any("не совпадает" in m for m in result.mismatches)

    def test_verify_fails_file_missing_on_disk(self, tmp_path: Path):
        """Верификация падает: файл есть в манифесте, но нет на диске."""
        module_dir = tmp_path / "test_module"
        module_dir.mkdir()
        _create_module_file(module_dir, "__init__.py", "# init")
        manifest = ModuleManifest(
            version="1.0.0",
            files={"__init__.py": "a" * 64, "missing.py": "b" * 64},
            manifest_hash=_compute_hash(
                _canonical_json(
                    {"__init__.py": "a" * 64, "missing.py": "b" * 64}
                ).encode("utf-8")
            ),
        )

        result = verify_module(module_dir, manifest)

        assert result.passed is False
        assert any("отсутствует на диске" in m for m in result.mismatches)

    def test_verify_fails_file_not_in_manifest(self, tmp_path: Path):
        """Верификация падает: файл на диске, но нет в манифесте."""
        module_dir = tmp_path / "test_module"
        module_dir.mkdir()
        h = _create_module_file(module_dir, "__init__.py", "# init")
        _create_module_file(module_dir, "extra.py", "# extra")
        manifest = ModuleManifest(
            version="1.0.0",
            files={"__init__.py": h},
            manifest_hash=_compute_hash(
                _canonical_json({"__init__.py": h}).encode("utf-8")
            ),
        )

        result = verify_module(module_dir, manifest)

        assert result.passed is False
        assert any("отсутствует в манифесте" in m for m in result.mismatches)

    def test_verify_fails_manifest_hash_mismatch(self, tmp_path: Path):
        """Верификация падает: manifest_hash не совпадает с вычисленным."""
        module_dir = tmp_path / "test_module"
        module_dir.mkdir()
        h = _create_module_file(module_dir, "__init__.py", "# test")
        manifest = ModuleManifest(
            version="1.0.0",
            files={"__init__.py": h},
            manifest_hash="b" * 64,  # Неверный manifest_hash
        )

        result = verify_module(module_dir, manifest)

        assert result.passed is False
        assert any("manifest_hash" in m for m in result.mismatches)

    def test_verify_multiple_files(self, tmp_path: Path):
        """Верификация с несколькими файлами."""
        module_dir = tmp_path / "test_module"
        module_dir.mkdir()
        h1 = _create_module_file(module_dir, "__init__.py", "# init")
        h2 = _create_module_file(module_dir, "core.py", "# core logic")
        files = {"__init__.py": h1, "core.py": h2}
        manifest = ModuleManifest(
            version="2.0.0",
            files=files,
            manifest_hash=_compute_hash(
                _canonical_json(files).encode("utf-8")
            ),
        )

        result = verify_module(module_dir, manifest)

        assert result.passed is True
        assert result.mismatches == []

    def test_verify_result_has_all_fields(self, tmp_path: Path):
        """VerificationResult содержит все поля."""
        module_dir = tmp_path / "test_module"
        module_dir.mkdir()
        h = _create_module_file(module_dir, "__init__.py", "# test")
        manifest = ModuleManifest(
            version="1.0.0",
            files={"__init__.py": h},
            manifest_hash=_compute_hash(
                _canonical_json({"__init__.py": h}).encode("utf-8")
            ),
        )

        result = verify_module(module_dir, manifest)

        assert isinstance(result, VerificationResult)
        assert result.module_name == "test_module"
        assert result.manifest == manifest
        assert isinstance(result.runtime_hashes, dict)
        assert isinstance(result.mismatches, list)
        assert result.error is None


# ── Тесты вспомогательных функций ──────────────────────────────────


class TestHelperFunctions:
    """Тесты внутренних функций."""

    def test_canonical_json_deterministic(self):
        """Каноническое JSON детерминировано (порядок ключей)."""
        files = {"z.py": "hash_z", "a.py": "hash_a", "m.py": "hash_m"}
        result1 = _canonical_json(files)
        result2 = _canonical_json(files)
        assert result1 == result2
        # Ключи отсортированы
        assert result1.index("a.py") < result1.index("m.py")
        assert result1.index("m.py") < result1.index("z.py")

    def test_canonical_json_compact(self):
        """Каноническое JSON компактное (без пробелов)."""
        files = {"test.py": "abc"}
        result = _canonical_json(files)
        assert ": " not in result  # Нет пробелов после двоеточия
        assert ", " not in result   # Нет пробелов после запятой

    def test_compute_hash_correctness(self):
        """_compute_hash вычисляет SHA256 корректно."""
        data = b"hello world"
        expected = hashlib.sha256(data).hexdigest()
        assert _compute_hash(data) == expected


# ── Тесты compare_versions ──────────────────────────────────────────


class TestCompareVersions:
    """Тесты сверки версий модуля."""

    def test_versions_match_sha256_ok(self):
        """Версии совпадают, SHA256 ОК → None (без подсказки)."""
        result = compare_versions("mod", "1.0.0", "1.0.0", sha256_passed=True)
        assert result is None

    def test_versions_match_sha256_fail(self):
        """Версии совпадают, SHA256 не совпал → hint «без бампа версии»."""
        result = compare_versions("mod", "1.0.0", "1.0.0", sha256_passed=False)
        assert result is not None
        assert "бампа версии" in result.lower()
        assert "v1.0.0" in result
        assert "mod" in result

    def test_versions_differ(self):
        """Версии не совпадают → hint «код обновлён»."""
        result = compare_versions("mod", "1.0.0", "2.0.0", sha256_passed=False)
        assert result is not None
        assert "код обновлён до v2.0.0" in result
        assert "манифест для v1.0.0" in result
        assert "mod" in result

    def test_versions_differ_sha256_ok(self):
        """Версии не совпадают, SHA256 ОК (манифест устарел) → hint «код обновлён»."""
        result = compare_versions("mod", "1.0.0", "2.0.0", sha256_passed=True)
        assert result is not None
        assert "код обновлён до v2.0.0" in result

    def test_hint_contains_regenerate_command(self):
        """Hint при несовпадении версий содержит команду перегенерации."""
        result = compare_versions("my_mod", "1.0.0", "2.0.0", sha256_passed=False)
        assert result is not None
        assert "python scripts/generate_hash.py my_mod" in result

    def test_hint_contains_module_name(self):
        """Hint содержит имя модуля."""
        result = compare_versions("auth", "1.0.0", "2.0.0", sha256_passed=False)
        assert "auth" in result

    def test_no_bump_hint_contains_version(self):
        """Hint «без бампа версии» содержит текущую версию."""
        result = compare_versions("mod", "3.0.0", "3.0.0", sha256_passed=False)
        assert "v3.0.0" in result
        assert "перегенерируй hash.json" in result
