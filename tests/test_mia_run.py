"""Unit-тесты mia.run: всегда result-dict, без Celery FAILURE."""
from __future__ import annotations

import json

from core.dispatch.envelope import TaskRequest
from core.dispatch.errors import METHOD_NOT_FOUND, WORKER_NOT_READY
from core.dispatch.registry import TaskTargetRegistry
from core.dispatch.secret_box import SecretBox
from core.dispatch import tasks as mia_tasks


def _envelope(
    box: SecretBox,
    module: str,
    method: str,
    args: list,
    kwargs: dict | None = None,
    request_id: str | None = None,
) -> dict:
    import json as _json

    payload = box.encrypt(_json.dumps({"args": args, "kwargs": kwargs or {}}, separators=(",", ":")).encode())
    return TaskRequest(
        id="t1",
        module=module,
        method=method,
        task_type="cpu",
        timeout=5.0,
        payload_enc=payload,
        request_id=request_id,
    ).to_dict()


def test_not_ready_returns_ok_false() -> None:
    mia_tasks._box = None
    mia_tasks._registry = None
    result = mia_tasks.mia_run({
        "v": 1,
        "id": "x",
        "module": "a",
        "method": "b",
        "task_type": "cpu",
        "timeout": 1.0,
        "payload_enc": "e",
    })
    assert result["ok"] is False
    assert result["error"]["code"] == WORKER_NOT_READY


def test_success_encrypts_result() -> None:
    box = SecretBox(bytes.fromhex("2222222222222222222222222222222222222222222222222222222222222222"))
    registry = TaskTargetRegistry()
    registry.register("demo", "add", lambda a, b: a + b)
    mia_tasks._box = box
    mia_tasks._registry = registry
    result = mia_tasks.mia_run(_envelope(box, "demo", "add", [2, 3]))
    assert result["ok"] is True
    assert json.loads(box.decrypt(result["result_enc"])) == 5


def test_missing_method_is_ok_false() -> None:
    box = SecretBox(bytes.fromhex("2222222222222222222222222222222222222222222222222222222222222222"))
    mia_tasks._box = box
    mia_tasks._registry = TaskTargetRegistry()
    result = mia_tasks.mia_run(_envelope(box, "demo", "nope", []))
    assert result["ok"] is False
    assert result["error"]["code"] == METHOD_NOT_FOUND


def test_request_id_logged_in_mia_run_ok(caplog) -> None:
    import logging

    box = SecretBox(bytes.fromhex("2222222222222222222222222222222222222222222222222222222222222222"))
    registry = TaskTargetRegistry()
    registry.register("demo", "add", lambda a, b: a + b)
    mia_tasks._box = box
    mia_tasks._registry = registry
    with caplog.at_level(logging.INFO, logger="core.dispatch.tasks"):
        result = mia_tasks.mia_run(_envelope(box, "demo", "add", [2, 3], request_id="rid-9"))
    assert result["ok"] is True
    logged = [r for r in caplog.records if r.getMessage() in ("mia_run_start", "mia_run_ok")]
    assert len(logged) == 2
    assert all(getattr(r, "request_id", None) == "rid-9" for r in logged)


def test_task_failed_is_ok_false() -> None:
    box = SecretBox(bytes.fromhex("2222222222222222222222222222222222222222222222222222222222222222"))
    registry = TaskTargetRegistry()

    def boom() -> None:
        raise RuntimeError("explode")

    registry.register("demo", "boom", boom)
    mia_tasks._box = box
    mia_tasks._registry = registry
    result = mia_tasks.mia_run(_envelope(box, "demo", "boom", []))
    assert result["ok"] is False
    assert result["error"]["code"] == "TASK_FAILED"
    assert result["error"]["message"] == "task failed"
    assert "explode" not in result["error"]["message"]
