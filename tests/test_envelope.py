"""Unit-тесты envelope codec."""
from __future__ import annotations

from core.dispatch.envelope import TaskRequest, TaskResult


def test_request_roundtrip() -> None:
    req = TaskRequest(
        id="t1",
        module="auth",
        method="login",
        task_type="database",
        timeout=10.0,
        payload_enc="abc",
    )
    restored = TaskRequest.from_dict(req.to_dict())
    assert restored.id == "t1"
    assert restored.module == "auth"
    assert restored.method == "login"
    assert restored.payload_enc == "abc"
    assert restored.v == 1


def test_request_json_roundtrip() -> None:
    req = TaskRequest(
        id="t2",
        module="db",
        method="_provider_get",
        task_type="database",
        timeout=5.0,
        payload_enc="xyz",
    )
    restored = TaskRequest.from_json(req.to_json())
    assert restored.to_dict() == req.to_dict()


def test_request_without_request_id_is_v1_compatible() -> None:
    """Старый envelope не меняется: request_id=None не сериализуется."""
    req = TaskRequest(
        id="t3", module="db", method="m", task_type="database",
        timeout=5.0, payload_enc="p",
    )
    data = req.to_dict()
    assert "request_id" not in data
    assert data["v"] == 1
    assert TaskRequest.from_dict(data).request_id is None


def test_request_id_roundtrip() -> None:
    req = TaskRequest(
        id="t4", module="db", method="m", task_type="database",
        timeout=5.0, payload_enc="p", request_id="rid-42",
    )
    restored = TaskRequest.from_json(req.to_json())
    assert restored.request_id == "rid-42"
    assert restored.v == 1


def test_result_ok_dict() -> None:
    result = TaskResult.ok_enc("cipher")
    data = result.to_dict()
    assert data == {"ok": True, "result_enc": "cipher"}
    assert TaskResult.from_dict(data).result_enc == "cipher"


def test_result_error_dict() -> None:
    result = TaskResult.fail("TASK_FAILED", "boom", "RuntimeError")
    data = result.to_dict()
    assert data["ok"] is False
    assert data["error"]["code"] == "TASK_FAILED"
    assert data["error"]["message"] == "boom"
    assert data["error"]["type"] == "RuntimeError"
