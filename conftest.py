"""Root conftest — регистрирует фейковые пакеты для подмодулей.

Модули теперь без дефисов (llm вместо mia-llm), но providers/ и auth-зависимости
всё ещё нуждаются в ручной регистрации.
"""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

_project_root = Path(__file__).resolve().parent


# ── Глобальный mock SmartDispatcher для тестов ────────────────

class _FakeWorkerManager:
    """Синхронный WorkerManager для тестов."""

    def __init__(self) -> None:
        self.submitted: list = []

    def submit(self, fn, *args, **kwargs):
        self.submitted.append((fn, args, kwargs))
        return fn(*args, **kwargs)


@pytest.fixture(autouse=True)
def _global_dispatcher():
    """Установить mock SmartDispatcher для всех тестов.

    Гарантирует что @task и @db_method декораторы работают
    даже без Application.startup().
    """
    from core.task_decorator import set_global_dispatcher
    from core.shared_memory import SharedMemory
    from pools.smart_dispatcher import SmartDispatcher

    wm = _FakeWorkerManager()
    sm = SharedMemory(backend="local", num_blocks=16, block_size=4096)
    sm.start()
    dp = SmartDispatcher(wm, shared_memory=sm)
    set_global_dispatcher(dp)
    yield dp
    set_global_dispatcher(None)
    sm.shutdown()


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

    # Регистрируем алиас с подчёркиванием (modules.llm = modules.llm)
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


# ── LLM: providers subpackage (нужна отдельная регистрация) ──
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
