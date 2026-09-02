"""Ошибки диспетчера задач."""
from __future__ import annotations

from core.errors import MiaError

WORKER_NOT_READY = "WORKER_NOT_READY"
PAYLOAD_FORBIDDEN = "PAYLOAD_FORBIDDEN"
PAYLOAD_NOT_SERIALIZABLE = "PAYLOAD_NOT_SERIALIZABLE"
CRYPTO_KEY_MISSING = "CRYPTO_KEY_MISSING"
CRYPTO_DECRYPT_FAILED = "CRYPTO_DECRYPT_FAILED"
METHOD_NOT_FOUND = "METHOD_NOT_FOUND"
TASK_FAILED = "TASK_FAILED"
REDIS_PROTOCOL = "REDIS_PROTOCOL"


class DispatchError(MiaError):
    """Ошибка диспетчера: код + сообщение, без секретов."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")
