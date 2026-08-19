"""AES-256-GCM: ключ только из ENV, секреты в лог не пишем."""
from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from core.dispatch.errors import CRYPTO_DECRYPT_FAILED, CRYPTO_KEY_MISSING, DispatchError

_KEY_ENV = "MIA_TASK_CRYPTO_KEY"
_OLD_KEY_ENV = "MIA_TASK_CRYPTO_KEY_OLD"
_VERSION = 1
_NONCE_SIZE = 12
_TAG_SIZE = 16
_KEY_BYTES = 32
_KEY_HEX_LEN = 64


class SecretBox:
    """Шифрует payload до send. Формат: version(1)+nonce(12)+ct+tag(16) → b64."""

    def __init__(self, key: bytes, old_key: bytes | None = None) -> None:
        if len(key) != _KEY_BYTES:
            raise DispatchError(CRYPTO_KEY_MISSING, "crypto key must be 32 bytes")
        if old_key is not None and len(old_key) != _KEY_BYTES:
            raise DispatchError(CRYPTO_KEY_MISSING, "old crypto key must be 32 bytes")
        self._key = key
        self._old_key = old_key

    @classmethod
    def from_env(cls) -> SecretBox:
        """Прочитать ключ из ENV. Не логировать значение."""
        return cls(_parse_key(_require_key()), _parse_key_optional(os.environ.get(_OLD_KEY_ENV)))

    def encrypt(self, plaintext: bytes) -> str:
        nonce = os.urandom(_NONCE_SIZE)
        blob = bytes([_VERSION]) + nonce + AESGCM(self._key).encrypt(nonce, plaintext, None)
        return base64.b64encode(blob).decode("ascii")

    def decrypt(self, encoded: str) -> bytes:
        try:
            blob = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise DispatchError(CRYPTO_DECRYPT_FAILED, "invalid ciphertext encoding") from exc
        if len(blob) < 1 + _NONCE_SIZE + _TAG_SIZE:
            raise DispatchError(CRYPTO_DECRYPT_FAILED, "ciphertext too short")
        if blob[0] != _VERSION:
            raise DispatchError(CRYPTO_DECRYPT_FAILED, "unsupported crypto version")
        nonce = blob[1 : 1 + _NONCE_SIZE]
        body = blob[1 + _NONCE_SIZE :]
        for candidate in (self._key, self._old_key):
            if candidate is None:
                continue
            try:
                return AESGCM(candidate).decrypt(nonce, body, None)
            except Exception:
                continue
        raise DispatchError(CRYPTO_DECRYPT_FAILED, "decrypt failed")


def _require_key() -> str:
    raw = os.environ.get(_KEY_ENV, "").strip()
    if not raw:
        raise DispatchError(CRYPTO_KEY_MISSING, f"{_KEY_ENV} is not set")
    return raw


def _parse_key(raw: str) -> bytes:
    if len(raw) != _KEY_HEX_LEN:
        raise DispatchError(CRYPTO_KEY_MISSING, "crypto key must be 64 hex chars")
    try:
        return bytes.fromhex(raw)
    except ValueError as exc:
        raise DispatchError(CRYPTO_KEY_MISSING, "crypto key is not valid hex") from exc


def _parse_key_optional(raw: str | None) -> bytes | None:
    if raw is None or not raw.strip():
        return None
    return _parse_key(raw.strip())
