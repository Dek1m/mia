"""Контракт envelope: request / result. JSON codec, без секретов в полях метаданных."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

ENVELOPE_VERSION = 1


@dataclass(frozen=True)
class TaskRequest:
    """Запрос mia.run. payload целиком в payload_enc."""

    id: str
    module: str
    method: str
    task_type: str
    timeout: float
    payload_enc: str
    v: int = ENVELOPE_VERSION
    # Correlation id HTTP-запроса (X-Request-Id). Опционален: v=1 совместим —
    # старые воркеры игнорируют поле, старые продюсеры его не пишут.
    request_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {
            "v": self.v,
            "id": self.id,
            "module": self.module,
            "method": self.method,
            "task_type": self.task_type,
            "timeout": self.timeout,
            "payload_enc": self.payload_enc,
        }
        if self.request_id:
            data["request_id"] = self.request_id
        return data

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskRequest:
        raw_request_id = data.get("request_id")
        return cls(
            v=int(data.get("v", ENVELOPE_VERSION)),
            id=str(data["id"]),
            module=str(data["module"]),
            method=str(data["method"]),
            task_type=str(data["task_type"]),
            timeout=float(data["timeout"]),
            payload_enc=str(data["payload_enc"]),
            request_id=str(raw_request_id) if raw_request_id else None,
        )

    @classmethod
    def from_json(cls, text: str) -> TaskRequest:
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError("envelope must be an object")
        return cls.from_dict(parsed)


@dataclass(frozen=True)
class TaskError:
    type: str
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"type": self.type, "code": self.code, "message": self.message}


@dataclass
class TaskResult:
    ok: bool
    result_enc: str | None = None
    error: TaskError | None = None

    def to_dict(self) -> dict[str, Any]:
        if self.ok:
            return {"ok": True, "result_enc": self.result_enc}
        err = self.error or TaskError("DispatchError", "TASK_FAILED", "unknown error")
        return {"ok": False, "error": err.to_dict()}

    @classmethod
    def ok_enc(cls, result_enc: str) -> TaskResult:
        return cls(ok=True, result_enc=result_enc)

    @classmethod
    def fail(cls, code: str, message: str, error_type: str = "DispatchError") -> TaskResult:
        return cls(ok=False, error=TaskError(type=error_type, code=code, message=message))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskResult:
        if data.get("ok"):
            return cls(ok=True, result_enc=data.get("result_enc"))
        raw = data.get("error") or {}
        return cls.fail(
            code=str(raw.get("code", "TASK_FAILED")),
            message=str(raw.get("message", "")),
            error_type=str(raw.get("type", "DispatchError")),
        )
