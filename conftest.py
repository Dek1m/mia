"""Root conftest — LocalInvokeDispatcher для unit-тестов."""
import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest

os.environ.setdefault("MIA_DISPATCH", "local")
os.environ.setdefault("MIA_TASK_CRYPTO_KEY", "00" * 32)

_project_root = Path(__file__).resolve().parent


@pytest.fixture(autouse=True)
def _global_dispatcher():
    """@task и модули не ходят в Redis."""
    from core.dispatch.local import LocalInvokeDispatcher
    from core.task_decorator import set_global_dispatcher

    dispatcher = LocalInvokeDispatcher()
    set_global_dispatcher(dispatcher)
    yield dispatcher
    set_global_dispatcher(None)


def _register_package(pkg_name: str, module_dir: Path, submodules: list[str] | None = None) -> None:
    """Зарегистрировать пакет с подмодулями в sys.modules."""
    dotted = f"modules.{pkg_name}"

    if "modules" not in sys.modules:
        _fake_modules = types.ModuleType("modules")
        _fake_modules.__path__ = [str(module_dir.parent)]  # type: ignore[attr-defined]
        _fake_modules.__package__ = "modules"
        sys.modules["modules"] = _fake_modules

    _fake_pkg = types.ModuleType(dotted)
    _fake_pkg.__path__ = [str(module_dir)]  # type: ignore[attr-defined]
    _fake_pkg.__package__ = dotted
    sys.modules[dotted] = _fake_pkg

    python_name = pkg_name.replace("-", "_")
    if python_name != pkg_name:
        sys.modules[python_name] = _fake_pkg

    if submodules:
        for submod in submodules:
            file_path = module_dir / f"{submod}.py"
            if not file_path.exists():
                continue
            spec = importlib.util.spec_from_file_location(
                f"{dotted}.{submod}", file_path,
            )
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                mod.__package__ = dotted
                sys.modules[f"{dotted}.{submod}"] = mod
                if python_name != pkg_name:
                    sys.modules[f"modules.{python_name}.{submod}"] = mod
                spec.loader.exec_module(mod)
                setattr(_fake_pkg, submod, mod)


_llm_dir = _project_root / "modules" / "llm"
_providers_dir = _llm_dir / "providers"
if _providers_dir.exists() and "modules.llm.providers" not in sys.modules:
    _fake_providers = types.ModuleType("modules.llm.providers")
    _fake_providers.__path__ = [str(_providers_dir)]
    _fake_providers.__package__ = "modules.llm.providers"
    sys.modules["modules.llm.providers"] = _fake_providers

    for submod in ["base", "openai", "registry"]:
        file_path = _providers_dir / f"{submod}.py"
        if file_path.exists():
            spec = importlib.util.spec_from_file_location(
                f"modules.llm.providers.{submod}", file_path,
            )
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                mod.__package__ = "modules.llm.providers"
                sys.modules[f"modules.llm.providers.{submod}"] = mod
                spec.loader.exec_module(mod)
                setattr(_fake_providers, submod, mod)
