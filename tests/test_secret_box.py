"""Unit-тесты SecretBox: AES-256-GCM, ENV, ротация ключа."""
from __future__ import annotations

import pytest

from core.dispatch.errors import CRYPTO_DECRYPT_FAILED, CRYPTO_KEY_MISSING, DispatchError
from core.dispatch.secret_box import SecretBox

_KEY = "abababababababababababababababababababababababababababababababab"
_OLD = "cdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd"


@pytest.fixture(autouse=True)
def _clean_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MIA_TASK_CRYPTO_KEY", raising=False)
    monkeypatch.delenv("MIA_TASK_CRYPTO_KEY_OLD", raising=False)


def test_from_env_missing_raises() -> None:
    with pytest.raises(DispatchError) as exc:
        SecretBox.from_env()
    assert exc.value.code == CRYPTO_KEY_MISSING


def test_from_env_invalid_hex(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIA_TASK_CRYPTO_KEY", "zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz")
    with pytest.raises(DispatchError) as exc:
        SecretBox.from_env()
    assert exc.value.code == CRYPTO_KEY_MISSING


def test_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIA_TASK_CRYPTO_KEY", _KEY)
    box = SecretBox.from_env()
    token = box.encrypt(b'{"args":[1]}')
    assert box.decrypt(token) == b'{"args":[1]}'
    assert token != "eyJhcmdzIjpbMV19"


def test_old_key_decrypts(monkeypatch: pytest.MonkeyPatch) -> None:
    old_box = SecretBox(bytes.fromhex(_OLD))
    token = old_box.encrypt(b"legacy")
    monkeypatch.setenv("MIA_TASK_CRYPTO_KEY", _KEY)
    monkeypatch.setenv("MIA_TASK_CRYPTO_KEY_OLD", _OLD)
    box = SecretBox.from_env()
    assert box.decrypt(token) == b"legacy"


def test_wrong_key_fails() -> None:
    box = SecretBox(bytes.fromhex(_KEY))
    other = SecretBox(bytes.fromhex(_OLD))
    token = box.encrypt(b"secret")
    with pytest.raises(DispatchError) as exc:
        other.decrypt(token)
    assert exc.value.code == CRYPTO_DECRYPT_FAILED


def test_garbage_ciphertext() -> None:
    box = SecretBox(bytes.fromhex(_KEY))
    with pytest.raises(DispatchError) as exc:
        box.decrypt("not-valid-base64!!!")
    assert exc.value.code == CRYPTO_DECRYPT_FAILED
