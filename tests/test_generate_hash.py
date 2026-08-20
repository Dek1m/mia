"""Тесты для scripts/generate_hash.py — CLI генерации hash.json.

Покрывает:
- generate_hash(): генерация для одного модуля
- main(): CLI с флагами --all, --modules-dir
- Валидность сгенерированного hash.json
- Исключение hash.json из files
- Отказ от symlink
"""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest

# Импорт функции generate_hash и _extract_version
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.generate_hash import generate_hash, _extract_version, main


# ── Вспомогательные функции ────────────────────────────────────────


def _compute_hash(data: str | bytes) -> str:
    """SHA256 hexdigest."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _create_module(
    modules_dir: Path,
    name: str,
    *,
    content: str | None = None,
    version: str = "1.0.0",
    extra_files: dict[str, str] | None = None,
) -> Path:
    """Создать тестовый модуль."""
    mod_dir = modules_dir / name
    mod_dir.mkdir(parents=True, exist_ok=True)

    if content is None:
        content = (
            f'__version__ = "{version}"\n\n'
            'from modules_system.module_base import ModuleBase\n\n'
            f'class TestModule(ModuleBase):\n'
            f'    @property\n'
            f'    def name(self) -> str:\n'
            f'        return "{name}"\n'
        )

    (mod_dir / "__init__.py").write_text(content, encoding="utf-8")

    if extra_files:
        for fname, fcontent in extra_files.items():
            fpath = mod_dir / fname
            fpath.parent.mkdir(parents=True, exist_ok=True)
            fpath.write_text(fcontent, encoding="utf-8")

    return mod_dir


def _read_hash_json(module_dir: Path) -> dict:
    """Прочитать и распарсить hash.json."""
    return json.loads((module_dir / "hash.json").read_text(encoding="utf-8"))


# ── Тесты _extract_version ─────────────────────────────────────────


class TestExtractVersion:
    """Тесты извлечения версии из __init__.py."""

    def test_extract_version_double_quotes(self, tmp_path: Path):
        """Версия в двойных кавычках."""
        mod_dir = tmp_path / "mod"
        mod_dir.mkdir()
        (mod_dir / "__init__.py").write_text('__version__ = "2.1.0"', encoding="utf-8")

        assert _extract_version(mod_dir) == "2.1.0"

    def test_extract_version_single_quotes(self, tmp_path: Path):
        """Версия в одинарных кавычках."""
        mod_dir = tmp_path / "mod"
        mod_dir.mkdir()
        (mod_dir / "__init__.py").write_text("__version__ = '3.0.1'", encoding="utf-8")

        assert _extract_version(mod_dir) == "3.0.1"

    def test_extract_module_version_double_quotes(self, tmp_path: Path):
        """MODULE_VERSION в двойных кавычках."""
        mod_dir = tmp_path / "mod"
        mod_dir.mkdir()
        (mod_dir / "__init__.py").write_text('MODULE_VERSION = "1.2.3"', encoding="utf-8")

        assert _extract_version(mod_dir) == "1.2.3"

    def test_extract_module_version_single_quotes(self, tmp_path: Path):
        """MODULE_VERSION в одинарных кавычках."""
        mod_dir = tmp_path / "mod"
        mod_dir.mkdir()
        (mod_dir / "__init__.py").write_text("MODULE_VERSION = '2.0.0'", encoding="utf-8")

        assert _extract_version(mod_dir) == "2.0.0"

    def test_extract_version_no_version(self, tmp_path: Path):
        """Нет ни __version__, ни MODULE_VERSION → 0.0.0."""
        mod_dir = tmp_path / "mod"
        mod_dir.mkdir()
        (mod_dir / "__init__.py").write_text("# no version here", encoding="utf-8")

        assert _extract_version(mod_dir) == "0.0.0"

    def test_extract_version_no_init(self, tmp_path: Path):
        """Нет __init__.py → 0.0.0."""
        mod_dir = tmp_path / "mod"
        mod_dir.mkdir()

        assert _extract_version(mod_dir) == "0.0.0"

    def test_extract_version_both_vars___version__first(self, tmp_path: Path):
        """Обе переменные: __version__ встречается первым → берётся __version__.

        Приоритет определяется позицией в файле (первое вхождение).
        """
        mod_dir = tmp_path / "mod"
        mod_dir.mkdir()
        content = (
            '__version__ = "1.1.1"\n'
            'MODULE_VERSION = "2.2.2"\n'
        )
        (mod_dir / "__init__.py").write_text(content, encoding="utf-8")

        assert _extract_version(mod_dir) == "1.1.1"

    def test_extract_version_both_vars_module_version_first(self, tmp_path: Path):
        """Обе переменные: MODULE_VERSION встречается первым → берётся MODULE_VERSION.

        Приоритет определяется позицией в файле (первое вхождение).
        """
        mod_dir = tmp_path / "mod"
        mod_dir.mkdir()
        content = (
            'MODULE_VERSION = "3.3.3"\n'
            '__version__ = "4.4.4"\n'
        )
        (mod_dir / "__init__.py").write_text(content, encoding="utf-8")

        assert _extract_version(mod_dir) == "3.3.3"

    def test_extract_version_module_version_real_modules(self):
        """Реальные модули: MODULE_VERSION извлекается корректно.

        Проверяем что generate_hash извлекает версию из реальных модулей.
        auth, db, sample используют MODULE_VERSION.
        """
        modules_dir = Path(__file__).resolve().parent.parent / "modules"

        for name, expected in [("auth", "2.0.0"), ("sample", "1.0.0")]:
            mod_dir = modules_dir / name
            if mod_dir.exists():
                assert _extract_version(mod_dir) == expected, (
                    f"Модуль {name}: ожидалась версия {expected}"
                )


# ── Тесты generate_hash ────────────────────────────────────────────


class TestGenerateHash:
    """Тесты функции generate_hash."""

    def test_generate_hash_creates_file(self, tmp_path: Path):
        """generate_hash создаёт hash.json."""
        mod_dir = _create_module(tmp_path, "testmod")

        generate_hash(mod_dir)

        assert (mod_dir / "hash.json").exists()

    def test_generate_hash_valid_json(self, tmp_path: Path):
        """Сгенерированный hash.json — валидный JSON."""
        mod_dir = _create_module(tmp_path, "testmod")

        generate_hash(mod_dir)

        data = _read_hash_json(mod_dir)
        assert isinstance(data, dict)
        assert "version" in data
        assert "files" in data
        assert "manifest_hash" in data

    def test_generate_hash_correct_version(self, tmp_path: Path):
        """Версия в hash.json берётся из __init__.py."""
        mod_dir = _create_module(tmp_path, "testmod", version="2.3.4")

        generate_hash(mod_dir)

        data = _read_hash_json(mod_dir)
        assert data["version"] == "2.3.4"

    def test_generate_hash_default_version(self, tmp_path: Path):
        """Если нет __version__ → версия 0.0.0."""
        mod_dir = tmp_path / "testmod"
        mod_dir.mkdir()
        (mod_dir / "__init__.py").write_text(
            "from modules_system.module_base import ModuleBase\n"
            "class M(ModuleBase):\n"
            "    @property\n"
            "    def name(self): return 'm'\n",
            encoding="utf-8",
        )

        generate_hash(mod_dir)

        data = _read_hash_json(mod_dir)
        assert data["version"] == "0.0.0"

    def test_generate_hash_excludes_itself(self, tmp_path: Path):
        """hash.json не включён в секцию files."""
        mod_dir = _create_module(tmp_path, "testmod")

        # Сначала создаём «старый» hash.json
        (mod_dir / "hash.json").write_text('{"old": true}', encoding="utf-8")

        generate_hash(mod_dir)

        data = _read_hash_json(mod_dir)
        assert "hash.json" not in data["files"]

    def test_generate_hash_includes_python_files(self, tmp_path: Path):
        """*.py файлы включены в files."""
        mod_dir = _create_module(
            tmp_path,
            "testmod",
            extra_files={"utils.py": "# utils code"},
        )

        generate_hash(mod_dir)

        data = _read_hash_json(mod_dir)
        assert "__init__.py" in data["files"]
        assert "utils.py" in data["files"]

    def test_generate_hash_includes_pyproject(self, tmp_path: Path):
        """pyproject.toml включён в files."""
        mod_dir = _create_module(tmp_path, "testmod")
        (mod_dir / "pyproject.toml").write_text(
            "[project]\nname = 'testmod'", encoding="utf-8"
        )

        generate_hash(mod_dir)

        data = _read_hash_json(mod_dir)
        assert "pyproject.toml" in data["files"]

    def test_generate_hash_includes_requirements(self, tmp_path: Path):
        """requirements*.txt включены в files."""
        mod_dir = _create_module(tmp_path, "testmod")
        (mod_dir / "requirements.txt").write_text("pytest", encoding="utf-8")
        (mod_dir / "requirements-dev.txt").write_text("black", encoding="utf-8")

        generate_hash(mod_dir)

        data = _read_hash_json(mod_dir)
        assert "requirements.txt" in data["files"]
        assert "requirements-dev.txt" in data["files"]

    def test_generate_hash_excludes_pycache(self, tmp_path: Path):
        """__pycache__/ исключён из files."""
        mod_dir = _create_module(tmp_path, "testmod")
        pycache = mod_dir / "__pycache__"
        pycache.mkdir()
        (pycache / "test.cpython-310.pyc").write_bytes(b"\x00")

        generate_hash(mod_dir)

        data = _read_hash_json(mod_dir)
        assert not any("__pycache__" in k for k in data["files"])

    def test_generate_hash_excludes_md(self, tmp_path: Path):
        """*.md файлы исключены из files."""
        mod_dir = _create_module(tmp_path, "testmod")
        (mod_dir / "README.md").write_text("# Test", encoding="utf-8")

        generate_hash(mod_dir)

        data = _read_hash_json(mod_dir)
        assert "README.md" not in data["files"]

    def test_generate_hash_manifest_hash_is_valid(self, tmp_path: Path):
        """manifest_hash — валидный SHA256 (64 hex символа)."""
        mod_dir = _create_module(tmp_path, "testmod")

        generate_hash(mod_dir)

        data = _read_hash_json(mod_dir)
        assert len(data["manifest_hash"]) == 64
        int(data["manifest_hash"], 16)  # Не вызовет ошибку если hex

    def test_generate_hash_all_files_have_valid_hashes(self, tmp_path: Path):
        """Все хеши в files — 64 hex символа."""
        mod_dir = _create_module(
            tmp_path,
            "testmod",
            extra_files={"helper.py": "# helper"},
        )

        generate_hash(mod_dir)

        data = _read_hash_json(mod_dir)
        for fname, h in data["files"].items():
            assert len(h) == 64, f"Хеш файла '{fname}' не 64 символа"
            int(h, 16)  # hex check

    def test_generate_hash_symlink_dir_rejected(self, tmp_path: Path):
        """Symlink-директория модуля → ValueError."""
        real_dir = tmp_path / "real"
        _create_module(tmp_path, "real")
        symlink = tmp_path / "link"
        symlink.symlink_to(real_dir)

        with pytest.raises(ValueError, match="symlink"):
            generate_hash(symlink)

    def test_generate_hash_nonexistent_dir(self, tmp_path: Path):
        """Несуществующая директория → FileNotFoundError."""
        fake_dir = tmp_path / "nonexistent"

        with pytest.raises(FileNotFoundError):
            generate_hash(fake_dir)

    def test_generate_hash_deterministic(self, tmp_path: Path):
        """Два вызова generate_hash для одного модуля → одинаковый hash.json."""
        mod_dir = _create_module(tmp_path, "testmod")

        generate_hash(mod_dir)
        data1 = _read_hash_json(mod_dir)

        generate_hash(mod_dir)
        data2 = _read_hash_json(mod_dir)

        assert data1 == data2

    def test_generate_hash_with_subdirs(self, tmp_path: Path):
        """Модуль с поддиректориями — хеширует всё дерево."""
        mod_dir = _create_module(
            tmp_path,
            "testmod",
            extra_files={"sub/deep.py": "# deep"},
        )

        generate_hash(mod_dir)

        data = _read_hash_json(mod_dir)
        assert "sub/deep.py" in data["files"]


# ── Тесты CLI main() ───────────────────────────────────────────────


class TestGenerateHashCLI:
    """Тесты CLI интерфейса main()."""

    def test_main_single_module(self, tmp_path: Path):
        """CLI: генерация для одного модуля."""
        _create_module(tmp_path, "testmod")
        modules_dir = tmp_path

        with patch("sys.argv", [
            "generate_hash.py", "testmod",
            "--modules-dir", str(modules_dir),
        ]):
            main()

        assert (modules_dir / "testmod" / "hash.json").exists()

    def test_main_all_modules(self, tmp_path: Path):
        """CLI: --all генерирует для всех модулей."""
        _create_module(tmp_path, "mod_a")
        _create_module(tmp_path, "mod_b")
        modules_dir = tmp_path

        with patch("sys.argv", [
            "generate_hash.py", "--all",
            "--modules-dir", str(modules_dir),
        ]):
            main()

        assert (modules_dir / "mod_a" / "hash.json").exists()
        assert (modules_dir / "mod_b" / "hash.json").exists()

    def test_main_nonexistent_modules_dir(self, tmp_path: Path):
        """CLI: несуществующая директория модулей → exit 1."""
        fake_dir = tmp_path / "no_such_dir"

        with patch("sys.argv", [
            "generate_hash.py", "--all",
            "--modules-dir", str(fake_dir),
        ]):
            with pytest.raises(SystemExit, match="1"):
                main()

    def test_main_no_args(self):
        """CLI: нет аргументов → exit 1."""
        with patch("sys.argv", ["generate_hash.py"]):
            with pytest.raises(SystemExit, match="1"):
                main()

    def test_main_symlink_module_fails(self, tmp_path: Path):
        """CLI: symlink-модуль → ошибка (не exit 1, а ошибка в errors)."""
        _create_module(tmp_path, "real")
        symlink = tmp_path / "link"
        symlink.symlink_to(tmp_path / "real")
        modules_dir = tmp_path

        with patch("sys.argv", [
            "generate_hash.py", "link",
            "--modules-dir", str(modules_dir),
        ]):
            with pytest.raises(SystemExit, match="1"):
                main()


# ── Тесты валидации сгенерированного hash.json ─────────────────────


class TestGeneratedHashValidation:
    """Проверка что сгенерированный hash.json проходит валидацию."""

    def test_generated_hash_passes_load_and_validate(self, tmp_path: Path):
        """Сгенерированный hash.json проходит load_and_validate_manifest."""
        from modules_system.verification import load_and_validate_manifest

        mod_dir = _create_module(tmp_path, "testmod")
        generate_hash(mod_dir)

        manifest = load_and_validate_manifest(mod_dir)
        assert manifest is not None
        assert manifest.version == "1.0.0"

    def test_generated_hash_passes_verify_module(self, tmp_path: Path):
        """Сгенерированный hash.json проходит verify_module."""
        from modules_system.verification import load_and_validate_manifest, verify_module

        mod_dir = _create_module(tmp_path, "testmod")
        generate_hash(mod_dir)

        manifest = load_and_validate_manifest(mod_dir)
        assert manifest is not None

        result = verify_module(mod_dir, manifest)
        assert result.passed is True
        assert result.mismatches == []

    def test_generate_rejects_symlink_file(self, tmp_path: Path):
        """generate_hash обнаруживает symlink-файл внутри модуля и отказывает."""
        mod_dir = _create_module(tmp_path, "testmod")
        # Создаём symlink на файл
        real_file = tmp_path / "external.py"
        real_file.write_text("# external", encoding="utf-8")
        symlink_file = mod_dir / "linked.py"
        symlink_file.symlink_to(real_file)

        # После фикса: generate_hash должен обнаружить symlink и отказаться
        with pytest.raises(ValueError, match="symlink"):
            generate_hash(mod_dir)


# ── Тесты валидации hash.json реальных модулей ─────────────────────


class TestRealModulesHashValidation:
    """Проверка что hash.json всех 6 модулей валидны."""

    @pytest.mark.parametrize("module_name,expected_version", [
        ("auth", "2.0.0"),
        ("db", "1.0.0"),
        ("llm", "1.0.0"),
        ("workspace", "1.0.0"),
        ("notifications", "0.0.0"),
        ("sample", "1.0.0"),
        ("apiproxy", "1.0.0"),
        ("cli", "1.0.0"),
    ])
    def test_hash_json_valid_and_correct_version(self, module_name, expected_version):
        """hash.json модуля валиден и содержит правильную версию."""
        from modules_system.verification import load_and_validate_manifest

        modules_dir = Path(__file__).resolve().parent.parent / "modules"
        module_dir = modules_dir / module_name

        if not module_dir.exists():
            pytest.skip(f"Модуль {module_name} не найден")

        manifest = load_and_validate_manifest(module_dir)
        assert manifest is not None, f"hash.json модуля {module_name} не загружается"
        assert manifest.version == expected_version, (
            f"Модуль {module_name}: версия {manifest.version}, ожидалась {expected_version}"
        )
        assert isinstance(manifest.files, dict)
        assert len(manifest.files) > 0, f"Модуль {module_name}: hash.json не содержит файлов"
        assert len(manifest.manifest_hash) == 64

    @pytest.mark.parametrize("module_name", [
        "auth", "db", "llm", "workspace", "notifications", "sample",
        "apiproxy", "cli",
    ])
    def test_hash_json_verify_module_passes(self, module_name):
        """verify_module для реального модуля проходит (файлы совпадают с хешами)."""
        from modules_system.verification import load_and_validate_manifest, verify_module

        modules_dir = Path(__file__).resolve().parent.parent / "modules"
        module_dir = modules_dir / module_name

        if not module_dir.exists():
            pytest.skip(f"Модуль {module_name} не найден")

        manifest = load_and_validate_manifest(module_dir)
        assert manifest is not None

        result = verify_module(module_dir, manifest)
        assert result.passed is True, (
            f"Модуль {module_name}: верификация не прошла: {result.mismatches}"
        )
