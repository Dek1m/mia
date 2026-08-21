"""Шифрование payload/result. Без содержимого в логах."""
from __future__ import annotations

import json
from typing import Any

from core.dispatch.errors import DispatchError
from core.dispatch.secret_box import SecretBox

MIA_TASK_NAME = "mia.run"
MIA_QUEUE = "mia"


def encode_result(box: SecretBox, value: Any) -> str:
    """Зашифровать результат. Не логировать содержимое."""
    return box.encrypt(
        json.dumps(value, separators=(",", ":"), default=str).encode("utf-8"),
    )


def decode_payload(box: SecretBox, payload_enc: str) -> dict[str, Any]:
    raw = json.loads(box.decrypt(payload_enc))
    if not isinstance(raw, dict):
        raise DispatchError("PAYLOAD_NOT_SERIALIZABLE", "payload must be an object")
    return raw
