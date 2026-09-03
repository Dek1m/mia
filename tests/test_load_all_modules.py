"""ADR-005: Application.load_all_modules — фильтр, rest last, collect hook."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.application import Application, process_role_from_env
from core.dispatch.local import LocalInvokeDispatcher
from modules_system.verification import VerificationMode


def _write_module(
    modules_dir,
    name: str,
    *,
    dependencies: list[str] | None = None,
    load_on: str = "all",
    is_example: bool = False,
    is_system: bool = False,
    extra_body: str = "",
) -> None:
    mod_dir = modules_dir / name
    mod_dir.mkdir(parents=True, exist_ok=True)
    deps = repr(dependencies or [])
    (mod_dir / "__init__.py").write_text(
        "from modules_system.module_base import ModuleBase, ModuleMeta\n\n"
        f"class {name.title()}Module(ModuleBase):\n"
        "    @property\n"
        "    def name(self) -> str:\n"
        f'        return "{name}"\n'
        "    @property\n"
        "    def version(self) -> str:\n"
        '        return "1.0.0"\n'
        "    @property\n"
        "    def meta(self) -> ModuleMeta:\n"
        "        return ModuleMeta(\n"
        f"            dependencies={deps},\n"
        f'            load_on="{load_on}",\n'
        f"            is_system={is_system},\n"
        f'            display_name="{name.title()}",\n'
        f"            is_example={is_example},\n"
        "        )\n"
        "    def on_load(self, state):\n"
        "        pass\n"
        f"{extra_body}\n",
        encoding="utf-8",
    )


def _app(modules_dir) -> Application:
    return Application(
        modules_dir=str(modules_dir),
        verification_mode=VerificationMode.DISABLED,
        dispatcher=LocalInvokeDispatcher(),
    )


class TestProcessRoleFromEnv:
    def test_belle_is_api(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SERVICE_NAME", "belle")
        assert process_role_from_env() == "api"

    def test_belle_worker_is_worker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SERVICE_NAME", "belle-worker")
        assert process_role_from_env() == "worker"

    def test_other_is_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SERVICE_NAME", raising=False)
        assert process_role_from_env() == "all"


class TestLoadAllModulesFilter:
    def test_sample_not_loaded(self, tmp_path) -> None:
        _write_module(tmp_path, "core")
        _write_module(tmp_path, "sample", is_example=True)
        app = _app(tmp_path)
        app.load_all_modules(role="api")
        loaded = app.modules.list_all()
        assert "core" in loaded
        assert "sample" not in loaded
        app.shutdown()

    def test_worker_skips_api_only(self, tmp_path) -> None:
        _write_module(tmp_path, "core")
        _write_module(tmp_path, "apiproxy", load_on="api", dependencies=["core"])
        _write_module(tmp_path, "worker", load_on="worker", dependencies=["core"])
        app = _app(tmp_path)
        app.load_all_modules(role="worker")
        loaded = app.modules.list_all()
        assert "core" in loaded
        assert "worker" in loaded
        assert "apiproxy" not in loaded
        app.shutdown()

    def test_api_skips_worker_only(self, tmp_path) -> None:
        _write_module(tmp_path, "core")
        _write_module(tmp_path, "worker", load_on="worker", dependencies=["core"])
        app = _app(tmp_path)
        app.load_all_modules(role="api")
        assert "worker" not in app.modules.list_all()
        app.shutdown()

    def test_role_all_skips_only_example(self, tmp_path) -> None:
        _write_module(tmp_path, "core")
        _write_module(tmp_path, "apiproxy", load_on="api", dependencies=["core"])
        _write_module(tmp_path, "sample", is_example=True)
        app = _app(tmp_path)
        app.load_all_modules(role="all")
        loaded = app.modules.list_all()
        assert "core" in loaded
        assert "apiproxy" in loaded
        assert "sample" not in loaded
        app.shutdown()


class TestRestLastAndCollect:
    def test_rest_loads_last_collect_sees_fs(self, tmp_path) -> None:
        """fs после apiproxy on_load — хук collect всё равно видит fs."""
        _write_module(tmp_path, "logmod")
        extra = (
            "    def on_load(self, state):\n"
            "        from types import SimpleNamespace\n"
            "        seen = list(state.modules.list_all())\n"
            "        self._on_load_seen = seen\n"
            "        class _Reg:\n"
            "            def collect_from_module(self, provider, name):\n"
            "                self.names.append(name)\n"
            "                return 1\n"
            "        reg = _Reg()\n"
            "        reg.names = []\n"
            "        self._provider = SimpleNamespace(registry=reg)\n"
        )
        _write_module(
            tmp_path,
            "apiproxy",
            dependencies=["logmod"],
            load_on="api",
            extra_body=extra,
        )
        _write_module(
            tmp_path,
            "fs",
            dependencies=["apiproxy"],
            extra_body=(
                "    def on_load(self, state):\n"
                "        self._provider = object()\n"
            ),
        )
        _write_module(
            tmp_path,
            "rest",
            dependencies=["apiproxy"],
            load_on="api",
        )

        app = _app(tmp_path)
        app.load_all_modules(role="api")
        loaded = app.modules.list_all()
        assert loaded[-1] == "rest"
        assert loaded.index("fs") < loaded.index("rest")
        proxy = app.modules.get("apiproxy")
        assert "fs" not in getattr(proxy, "_on_load_seen", [])
        collected = list(proxy._provider.registry.names)
        assert "fs" in collected
        app.shutdown()
