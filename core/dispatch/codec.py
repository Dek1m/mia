"""Шифрование payload/result. Без содержимого в логах."""
from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID

from core.dispatch.errors import DispatchError
from core.dispatch.secret_box import SecretBox

MIA_TASK_NAME = "mia.run"
MIA_QUEUE = "mia"


def _json_default(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def encode_result(box: SecretBox, value: Any) -> str:
    """Зашифровать результат. Не логировать содержимое."""
    return box.encrypt(
        json.dumps(value, separators=(",", ":"), default=_json_default).encode("utf-8"),
    )


def decode_payload(box: SecretBox, payload_enc: str) -> dict[str, Any]:
    raw = json.loads(box.decrypt(payload_enc))
    if not isinstance(raw, dict):
        raise DispatchError("PAYLOAD_NOT_SERIALIZABLE", "payload must be an object")
    return raw
