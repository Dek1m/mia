"""Тесты топологической сортировки модулей в ModuleManager.

Проверяет:
-discover_and_sort() возвращает модули в правильном порядке
- Обнаружение циклических зависимостей
- Корректное чтение dependencies из AST
- Edge cases: пустые зависимости, несуществующие модули
"""
from __future__ import annotations

import pytest

from modules_system.module_base import ModuleMeta, should_load
from modules_system.module_manager import ModuleManager
from modules_system.verification import VerificationMode


# ── Вспомогательные функции ────────────────────────────────────────


def _create_module(
    modules_dir,
    name: str,
    dependencies: list[str] | None = None,
    content: str | None = None,
) -> None:
    """Создать тестовый модуль с dependencies.

    Args:
        modules_dir: Родительская директория.
        name: Имя модуля.
        dependencies: Список зависимостей.
        content: Содержимое __init__.py. Если None — автогенерация.
    """
    mod_dir = modules_dir / name
    mod_dir.mkdir(parents=True, exist_ok=True)

    if content is None:
        deps_str = repr(dependencies) if dependencies else "[]"
        content = (
            'from modules_system.module_base import ModuleBase, ModuleMeta\n\n'
            f'class TestModule(ModuleBase):\n'
            f'    @property\n'
            f'    def name(self) -> str:\n'
            f'        return "{name}"\n'
            f'    @property\n'
            f'    def meta(self) -> ModuleMeta:\n'
            f'        return ModuleMeta(dependencies={deps_str})\n'
        )

    (mod_dir / "__init__.py").write_text(content, encoding="utf-8")


def _create_module_with_hash(
    modules_dir,
    name: str,
    dependencies: list[str] | None = None,
) -> None:
    """Создать модуль с hash.json для STRICT режима."""
    from tests.test_module_manager_verification import _create_test_module

    deps_str = repr(dependencies) if dependencies else "[]"
    content = (
        'from modules_system.module_base import ModuleBase, ModuleMeta\n\n'
        f'class TestModule(ModuleBase):\n'
        f'    @property\n'
        f'    def name(self) -> str:\n'
        f'        return "{name}"\n'
        f'    @property\n'
        f'    def meta(self) -> ModuleMeta:\n'
        f'        return ModuleMeta(dependencies={deps_str})\n'
    )
    _create_test_module(modules_dir, name=name, content=content, write_hash=False)


# ── Тесты discover_and_sort ────────────────────────────────────────


class TestDiscoverAndSort:
    """Тесты метода discover_and_sort."""

    def test_no_modules(self, tmp_path):
        """Пустая директория → пустой список."""
        mgr = ModuleManager(str(tmp_path), verification_mode=VerificationMode.DISABLED)
        result = mgr.discover_and_sort()
        assert result == []

    def test_single_module_no_deps(self, tmp_path):
        """Один модуль без зависимостей."""
        _create_module(tmp_path, "mod_a")
        mgr = ModuleManager(str(tmp_path), verification_mode=VerificationMode.DISABLED)

        result = mgr.discover_and_sort()

        assert result == ["mod_a"]

    def test_modules_without_dependencies(self, tmp_path):
        """Модули без зависимостей — сортировка по имени."""
        _create_module(tmp_path, "mod_c")
        _create_module(tmp_path, "mod_a")
        _create_module(tmp_path, "mod_b")
        mgr = ModuleManager(str(tmp_path), verification_mode=VerificationMode.DISABLED)

        result = mgr.discover_and_sort()

        assert result == ["mod_a", "mod_b", "mod_c"]

    def test_linear_dependency_chain(self, tmp_path):
        """Линейная цепочка: A → B → C (C зависит от B, B от A)."""
        _create_module(tmp_path, "mod_a")
        _create_module(tmp_path, "mod_b", dependencies=["mod_a"])
        _create_module(tmp_path, "mod_c", dependencies=["mod_b"])
        mgr = ModuleManager(str(tmp_path), verification_mode=VerificationMode.DISABLED)

        result = mgr.discover_and_sort()

        # mod_a должен быть قبل mod_b, mod_b перед mod_c
        assert result.index("mod_a") < result.index("mod_b")
        assert result.index("mod_b") < result.index("mod_c")

    def test_diamond_dependency(self, tmp_path):
        """Алмазная зависимость: D зависит от B и C, B и C от A."""
        _create_module(tmp_path, "mod_a")
        _create_module(tmp_path, "mod_b", dependencies=["mod_a"])
        _create_module(tmp_path, "mod_c", dependencies=["mod_a"])
        _create_module(tmp_path, "mod_d", dependencies=["mod_b", "mod_c"])
        mgr = ModuleManager(str(tmp_path), verification_mode=VerificationMode.DISABLED)

        result = mgr.discover_and_sort()

        # A перед B и C, B и C перед D
        assert result.index("mod_a") < result.index("mod_b")
        assert result.index("mod_a") < result.index("mod_c")
        assert result.index("mod_b") < result.index("mod_d")
        assert result.index("mod_c") < result.index("mod_d")

    def test_multiple_roots(self, tmp_path):
        """Нескорневые модули (несколько модулей без зависимостей)."""
        _create_module(tmp_path, "root_a")
        _create_module(tmp_path, "root_b")
        _create_module(tmp_path, "child", dependencies=["root_a", "root_b"])
        mgr = ModuleManager(str(tmp_path), verification_mode=VerificationMode.DISABLED)

        result = mgr.discover_and_sort()

        assert result.index("root_a") < result.index("child")
        assert result.index("root_b") < result.index("child")

    def test_cyclic_dependency_raises(self, tmp_path):
        """Циклические зависимости → ValueError."""
        _create_module(tmp_path, "mod_a", dependencies=["mod_b"])
        _create_module(tmp_path, "mod_b", dependencies=["mod_a"])
        mgr = ModuleManager(str(tmp_path), verification_mode=VerificationMode.DISABLED)

        with pytest.raises(ValueError, match="Циклические зависимости"):
            mgr.discover_and_sort()

    def test_self_dependency_cycle(self, tmp_path):
        """Модуль зависит от самого себя → цикл."""
        _create_module(tmp_path, "mod_self", dependencies=["mod_self"])
        mgr = ModuleManager(str(tmp_path), verification_mode=VerificationMode.DISABLED)

        with pytest.raises(ValueError, match="Циклические зависимости"):
            mgr.discover_and_sort()

    def test_three_node_cycle(self, tmp_path):
        """Цикл из трёх модулей: A → B → C → A."""
        _create_module(tmp_path, "mod_a", dependencies=["mod_c"])
        _create_module(tmp_path, "mod_b", dependencies=["mod_a"])
        _create_module(tmp_path, "mod_c", dependencies=["mod_b"])
        mgr = ModuleManager(str(tmp_path), verification_mode=VerificationMode.DISABLED)

        with pytest.raises(ValueError, match="Циклические зависимости"):
            mgr.discover_and_sort()

    def test_dependency_on_nonexistent_module(self, tmp_path):
        """Зависимость от несуществующего модуля — игнорируется."""
        _create_module(tmp_path, "mod_a", dependencies=["nonexistent"])
        mgr = ModuleManager(str(tmp_path), verification_mode=VerificationMode.DISABLED)

        result = mgr.discover_and_sort()

        # nonexistent не в списке модулей, но mod_a загружается
        assert result == ["mod_a"]

    def test_complex_graph(self, tmp_path):
        """Сложный граф: 6 модулей с разными зависимостями."""
        _create_module(tmp_path, "db")
        _create_module(tmp_path, "auth", dependencies=["db"])
        _create_module(tmp_path, "workspace", dependencies=["db"])
        _create_module(tmp_path, "llm", dependencies=["db"])
        _create_module(tmp_path, "apiproxy", dependencies=["auth", "workspace"])
        _create_module(tmp_path, "cli", dependencies=["apiproxy", "llm"])
        mgr = ModuleManager(str(tmp_path), verification_mode=VerificationMode.DISABLED)

        result = mgr.discover_and_sort()

        # db перед всеми
        assert result.index("db") == 0
        # auth, workspace, llm после db
        assert result.index("auth") > result.index("db")
        assert result.index("workspace") > result.index("db")
        assert result.index("llm") > result.index("db")
        # apiproxy после auth и workspace
        assert result.index("apiproxy") > result.index("auth")
        assert result.index("apiproxy") > result.index("workspace")
        # cli после apiproxy и llm
        assert result.index("cli") > result.index("apiproxy")
        assert result.index("cli") > result.index("llm")

    def test_whitelist_filtering(self, tmp_path):
        """Whitelist фильтрует модули перед сортировкой."""
        _create_module(tmp_path, "mod_a")
        _create_module(tmp_path, "mod_b", dependencies=["mod_a"])
        _create_module(tmp_path, "mod_c", dependencies=["mod_a"])
        mgr = ModuleManager(
            str(tmp_path),
            allowed_modules=["mod_a", "mod_c"],
            verification_mode=VerificationMode.DISABLED,
        )

        result = mgr.discover_and_sort()

        # mod_b не в whitelist — отфильтрован. mod_c依赖mod_a (который есть)
        assert "mod_a" in result
        assert "mod_c" in result
        assert "mod_b" not in result
        # mod_a перед mod_c
        assert result.index("mod_a") < result.index("mod_c")


# ── Тесты _read_meta ───────────────────────────────────────────────


class TestReadMeta:
    """Тесты метода _read_meta (AST парсинг)."""

    def test_read_meta_with_dependencies(self, tmp_path):
        """Чтение dependencies из __init__.py."""
        _create_module(tmp_path, "mod_a", dependencies=["db", "auth"])
        mgr = ModuleManager(str(tmp_path), verification_mode=VerificationMode.DISABLED)

        meta = mgr._read_meta("mod_a")

        assert meta.dependencies == ["db", "auth"]

    def test_read_meta_no_dependencies(self, tmp_path):
        """Модуль без dependencies → пустой список."""
        _create_module(tmp_path, "mod_a")
        mgr = ModuleManager(str(tmp_path), verification_mode=VerificationMode.DISABLED)

        meta = mgr._read_meta("mod_a")

        assert meta.dependencies == []

    def test_read_meta_nonexistent_module(self, tmp_path):
        """Несуществующий модуль → ModuleMeta() по умолчанию."""
        mgr = ModuleManager(str(tmp_path), verification_mode=VerificationMode.DISABLED)

        meta = mgr._read_meta("nonexistent")

        assert meta == ModuleMeta()

    def test_read_meta_invalid_syntax(self, tmp_path):
        """Невалидный синтаксис __init__.py → ModuleMeta() по умолчанию."""
        mod_dir = tmp_path / "bad_syntax"
        mod_dir.mkdir()
        (mod_dir / "__init__.py").write_text("def (:", encoding="utf-8")
        mgr = ModuleManager(str(tmp_path), verification_mode=VerificationMode.DISABLED)

        meta = mgr._read_meta("bad_syntax")

        assert meta == ModuleMeta()

    def test_read_meta_complex_dependencies(self, tmp_path):
        """Сложные зависимости: список строк."""
        content = (
            'from modules_system.module_base import ModuleBase, ModuleMeta\n\n'
            'class TestModule(ModuleBase):\n'
            '    @property\n'
            '    def name(self) -> str:\n'
            '        return "mod_complex"\n'
            '    @property\n'
            '    def meta(self) -> ModuleMeta:\n'
            '        return ModuleMeta(\n'
            '            dependencies=["db", "auth", "cache"],\n'
            '            timeout_defaults={"chat": 120.0},\n'
            '        )\n'
        )
        _create_module(tmp_path, "mod_complex", content=content)
        mgr = ModuleManager(str(tmp_path), verification_mode=VerificationMode.DISABLED)

        meta = mgr._read_meta("mod_complex")

        assert meta.dependencies == ["db", "auth", "cache"]
        assert meta.timeout_defaults == {"chat": 120.0}

    def test_read_meta_load_on_and_flags(self, tmp_path):
        """AST парсит load_on, is_system, is_example, display_name (str/bool)."""
        content = (
            'from modules_system.module_base import ModuleBase, ModuleMeta\n\n'
            'class TestModule(ModuleBase):\n'
            '    @property\n'
            '    def name(self) -> str:\n'
            '        return "apiproxy"\n'
            '    @property\n'
            '    def meta(self) -> ModuleMeta:\n'
            '        return ModuleMeta(\n'
            '            dependencies=["auth"],\n'
            '            load_on="api",\n'
            '            is_system=True,\n'
            '            is_example=False,\n'
            '            display_name="API Proxy",\n'
            '        )\n'
        )
        _create_module(tmp_path, "apiproxy", content=content)
        mgr = ModuleManager(str(tmp_path), verification_mode=VerificationMode.DISABLED)

        meta = mgr._read_meta("apiproxy")

        assert meta.dependencies == ["auth"]
        assert meta.load_on == "api"
        assert meta.is_system is True
        assert meta.is_example is False
        assert meta.display_name == "API Proxy"

    def test_read_meta_new_fields_defaults(self, tmp_path):
        """Старый ModuleMeta(dependencies=...) получает дефолты новых полей."""
        _create_module(tmp_path, "mod_a", dependencies=["db"])
        mgr = ModuleManager(str(tmp_path), verification_mode=VerificationMode.DISABLED)

        meta = mgr._read_meta("mod_a")

        assert meta.dependencies == ["db"]
        assert meta.load_on == "all"
        assert meta.is_system is False
        assert meta.is_example is False
        assert meta.display_name == ""


# ── Тесты _topological_sort ────────────────────────────────────────


class TestTopologicalSort:
    """Тесты алгоритма Kahn's."""

    def test_empty_graph(self):
        """Пустой граф."""
        mgr = ModuleManager("/tmp", verification_mode=VerificationMode.DISABLED)
        result = mgr._topological_sort({})
        assert result == []

    def test_single_node(self):
        """Одна вершина."""
        mgr = ModuleManager("/tmp", verification_mode=VerificationMode.DISABLED)
        result = mgr._topological_sort({"a": []})
        assert result == ["a"]

    def test_two_nodes_no_edge(self):
        """Две вершины без рёбер."""
        mgr = ModuleManager("/tmp", verification_mode=VerificationMode.DISABLED)
        result = mgr._topological_sort({"a": [], "b": []})
        assert set(result) == {"a", "b"}

    def test_two_nodes_with_edge(self):
        """Две вершины с ребром a → b."""
        mgr = ModuleManager("/tmp", verification_mode=VerificationMode.DISABLED)
        result = mgr._topological_sort({"a": [], "b": ["a"]})
        assert result == ["a", "b"]

    def test_preserves_independent_order(self):
        """Независимые вершины сохраняют исходный порядок."""
        mgr = ModuleManager("/tmp", verification_mode=VerificationMode.DISABLED)
        graph = {"c": [], "a": [], "b": []}
        result = mgr._topological_sort(graph)
        # Kahn's algorithm: очередь заполняется по порядку keys
        #依赖于 dict insertion order (Python 3.7+)
        assert result == ["c", "a", "b"]

    def test_cycle_raises(self):
        """Цикл → ValueError."""
        mgr = ModuleManager("/tmp", verification_mode=VerificationMode.DISABLED)
        graph = {"a": ["b"], "b": ["a"]}
        with pytest.raises(ValueError, match="Циклические зависимости"):
            mgr._topological_sort(graph)


# ── Тесты ModuleMeta с dependencies ────────────────────────────────


class TestModuleMetaDependencies:
    """Тесты ModuleMeta с полем dependencies."""

    def test_default_dependencies_is_empty_list(self):
        """По умолчанию dependencies — пустой список."""
        meta = ModuleMeta()
        assert meta.dependencies == []

    def test_dependencies_set_correctly(self):
        """Dependencies устанавливаются корректно."""
        meta = ModuleMeta(dependencies=["db", "auth"])
        assert meta.dependencies == ["db", "auth"]

    def test_dependencies_independence(self):
        """Dependencies не зависят от других полей."""
        meta = ModuleMeta(
            permissions={"login": "auth.login"},
            dependencies=["db"],
        )
        assert meta.dependencies == ["db"]
        assert meta.permissions == {"login": "auth.login"}

    def test_default_discovery_fields(self):
        """Новые поля ADR-005: all / False / пустой display_name."""
        meta = ModuleMeta()
        assert meta.load_on == "all"
        assert meta.is_system is False
        assert meta.is_example is False
        assert meta.display_name == ""


# ── Интеграционные тесты ──────────────────────────────────────────


class TestTopologicalIntegration:
    """Интеграционные тесты: discover_and_sort → load в правильном порядке."""

    def test_modules_load_in_topological_order(self, tmp_path):
        """Модули загружаются в топологическом порядке."""
        load_order = []

        # Создаём модули, которые при on_load записывают порядок
        for name, deps in [
            ("db", []),
            ("auth", ["db"]),
            ("workspace", ["db"]),
            ("apiproxy", ["auth", "workspace"]),
        ]:
            mod_dir = tmp_path / name
            mod_dir.mkdir()
            deps_str = repr(deps) if deps else "[]"
            content = (
                'from modules_system.module_base import ModuleBase, ModuleMeta\n'
                'import json\n'
                'from pathlib import Path\n\n'
                f'_LOAD_ORDER_FILE = Path("{tmp_path}/load_order.json")\n\n'
                f'class TestModule(ModuleBase):\n'
                f'    @property\n'
                f'    def name(self) -> str:\n'
                f'        return "{name}"\n'
                f'    @property\n'
                f'    def meta(self) -> ModuleMeta:\n'
                f'        return ModuleMeta(dependencies={deps_str})\n'
                f'    def on_load(self, state):\n'
                f'        order = []\n'
                f'        if _LOAD_ORDER_FILE.exists():\n'
                f'            order = json.loads(_LOAD_ORDER_FILE.read_text())\n'
                f'        order.append("{name}")\n'
                f'        _LOAD_ORDER_FILE.write_text(json.dumps(order))\n'
            )
            (mod_dir / "__init__.py").write_text(content, encoding="utf-8")

        mgr = ModuleManager(str(tmp_path), verification_mode=VerificationMode.DISABLED)
        sorted_modules = mgr.discover_and_sort()

        # Загружаем в отсортированном порядке
        for name in sorted_modules:
            mgr.load(name)

        # Проверяем порядок загрузки
        import json
        load_order = json.loads((tmp_path / "load_order.json").read_text())

        # db должен быть первым
        assert load_order[0] == "db"
        # auth и workspace после db
        assert load_order.index("auth") > load_order.index("db")
        assert load_order.index("workspace") > load_order.index("db")
        # apiproxy после auth и workspace
        assert load_order.index("apiproxy") > load_order.index("auth")
        assert load_order.index("apiproxy") > load_order.index("workspace")


# ── Тесты should_load (ADR-005) ────────────────────────────────────


class TestShouldLoad:
    """Фильтр загрузки по роли процесса."""

    def test_example_never_loads(self):
        """is_example → не грузить ни в api, ни в worker."""
        meta = ModuleMeta(is_example=True, load_on="all")
        assert should_load(meta, "api") is False
        assert should_load(meta, "worker") is False

    def test_load_on_all(self):
        """load_on=all → грузить в обоих ролях."""
        meta = ModuleMeta(load_on="all")
        assert should_load(meta, "api") is True
        assert should_load(meta, "worker") is True

    def test_load_on_api(self):
        """load_on=api → только belle REST."""
        meta = ModuleMeta(load_on="api")
        assert should_load(meta, "api") is True
        assert should_load(meta, "worker") is False

    def test_load_on_worker(self):
        """load_on=worker → только celery child."""
        meta = ModuleMeta(load_on="worker")
        assert should_load(meta, "api") is False
        assert should_load(meta, "worker") is True

    def test_example_wins_over_role(self):
        """is_example важнее совпадения load_on с role."""
        meta = ModuleMeta(load_on="api", is_example=True)
        assert should_load(meta, "api") is False
