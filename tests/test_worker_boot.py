"""ADR-005 шаг 6: worker boot без MIA_WORKER_MODULES."""
from __future__ import annotations

from types import SimpleNamespace

import core.dispatch.tasks as tasks


def test_tasks_source_has_no_worker_modules_env() -> None:
    import inspect

    source = inspect.getsource(tasks)
    assert "MIA_WORKER_MODULES" not in source
    assert "allowed_modules" not in source


def test_worker_service_name_from_env(monkeypatch) -> None:
    monkeypatch.setenv("SERVICE_NAME", "belle-worker")
    assert tasks._worker_service_name() == "belle-worker"
    monkeypatch.delenv("SERVICE_NAME", raising=False)
    assert tasks._worker_service_name() == "belle-worker"


def test_boot_worker_application_no_allowed_filter(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeApp:
        def __init__(self, **kwargs: object) -> None:
            captured["kwargs"] = kwargs
            self.modules = SimpleNamespace(list_all=lambda: ["db", "auth"], get=lambda n: None)
            self.database = object()

        def set_runtime_registry(self, registry: object) -> None:
            captured["registry"] = registry

        def load_all_modules(self, role: str | None = None) -> None:
            captured["role"] = role

        def publish_runtime(self) -> None:
            captured["published"] = True

    class FakeRuntime:
        def __init__(self, service: str) -> None:
            self.service = service
            captured["service"] = service

        @classmethod
        def from_env(cls, service: str | None = None) -> FakeRuntime:
            return cls(service or "missing")

        def start_heartbeat_loop(self) -> None:
            captured["hb"] = True

    monkeypatch.setenv("SERVICE_NAME", "belle-worker")
    monkeypatch.setenv("BELLE_MODULES_DIR", "/tmp/modules")
    monkeypatch.setattr("core.application.Application", FakeApp)
    monkeypatch.setattr(
        "modules_system.runtime_registry.ModuleRuntimeRegistry",
        FakeRuntime,
    )

    app = tasks.boot_worker_application()
    assert isinstance(app, FakeApp)
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert "allowed_modules" not in kwargs
    assert captured["role"] == "worker"
    assert captured["service"] == "belle-worker"
    assert captured["published"] is True
    assert captured["hb"] is True
