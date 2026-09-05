"""worker_process_init поднимает MetricsServer: env-порт, занятый порт не фатален."""
from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def worker_main(monkeypatch: pytest.MonkeyPatch):
    """modules.worker.__main__, защищённо от загрязнения sys.modules.

    Другие тесты (load_all_modules) подменяют пакет modules на tmp-каталог —
    здесь явно ставим реальные __path__ и выгружаем кеш modules.worker.*.
    """
    for name in [k for k in sys.modules if k == "modules.worker" or k.startswith("modules.worker.")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    for name, path in (("modules", _ROOT / "modules"), ("modules.worker", _ROOT / "modules" / "worker")):
        pkg = types.ModuleType(name)
        pkg.__path__ = [str(path)]  # type: ignore[attr-defined]
        pkg.__package__ = name
        monkeypatch.setitem(sys.modules, name, pkg)
    yield importlib.import_module("modules.worker.__main__")


def test_metrics_port_from_env_and_default(worker_main, monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[int] = []

    class FakeServer:
        def __init__(self, port: int) -> None:
            started.append(port)

        def start(self) -> None:
            pass

    monkeypatch.setattr("monitoring.metrics.MetricsServer", FakeServer)
    monkeypatch.setenv("MIA_METRICS_PORT", "9999")
    worker_main._on_process_init()
    monkeypatch.delenv("MIA_METRICS_PORT", raising=False)
    worker_main._on_process_init()
    assert started == [9999, 9100]


def test_metrics_port_busy_is_not_fatal(worker_main, monkeypatch: pytest.MonkeyPatch) -> None:
    class BusyServer:
        def __init__(self, port: int) -> None:
            pass

        def start(self) -> None:
            raise OSError("address already in use")

    monkeypatch.setattr("monitoring.metrics.MetricsServer", BusyServer)
    worker_main._on_process_init()  # не исключение: воркер продолжает работать


def test_prerun_logs_request_id(worker_main, caplog) -> None:
    """task_prerun прокидывает request_id из envelope в лог."""
    import logging

    envelope = {"id": "t1", "module": "llm", "method": "run_pipeline", "request_id": "rid-5"}
    with caplog.at_level(logging.INFO):
        worker_main._on_prerun(task_id="cel-1", task=None, args=(envelope,))
    prerun = [r for r in caplog.records if r.getMessage() == "task_prerun"]
    assert prerun and getattr(prerun[-1], "request_id", None) == "rid-5"
