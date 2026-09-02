"""TaskResultHandle: транспортные ошибки без маскировки Protocol error."""
from __future__ import annotations

from typing import Any

import pytest

from core.dispatch.errors import REDIS_PROTOCOL, TASK_FAILED, DispatchError
from core.dispatch.handle import TaskResultHandle
from core.dispatch.secret_box import SecretBox


class ProtocolError(Exception):
    pass


class _Result:
    def __init__(self, exc: BaseException | None = None, value: Any = None) -> None:
        self._exc = exc
        self._value = value
        self.get_calls = 0

    def get(self, timeout: float | None = None) -> Any:
        self.get_calls += 1
        if self._exc is not None:
            raise self._exc
        return self._value

    def ready(self) -> bool:
        return True

    def cancelled(self) -> bool:
        return False


@pytest.fixture
def box() -> SecretBox:
    return SecretBox(bytes.fromhex("11" * 32))


def test_redis_protocol_code(box: SecretBox) -> None:
    handle = TaskResultHandle(_Result(ProtocolError("Protocol Error: b'1'")), box)
    with pytest.raises(DispatchError) as exc:
        handle.result()
    assert exc.value.code == REDIS_PROTOCOL
    assert "Protocol Error: b'1'" in exc.value.message
    assert "Protocol error:" not in exc.value.message


def test_timeout_keeps_type_and_message(box: SecretBox) -> None:
    handle = TaskResultHandle(_Result(TimeoutError("timed out after 10s")), box)
    with pytest.raises(DispatchError) as exc:
        handle.result()
    assert exc.value.code == TASK_FAILED
    assert exc.value.message == "TimeoutError: timed out after 10s"


def test_nested_protocol_error(box: SecretBox) -> None:
    nested = ProtocolError("Protocol Error: b'1'")
    outer = RuntimeError("kombu failed")
    outer.__cause__ = nested
    handle = TaskResultHandle(_Result(outer), box)
    with pytest.raises(DispatchError) as exc:
        handle.result()
    assert exc.value.code == REDIS_PROTOCOL


def test_get_uses_lock(box: SecretBox) -> None:
    class _Lock:
        def __init__(self) -> None:
            self.entered = 0

        def __enter__(self) -> _Lock:
            self.entered += 1
            return self

        def __exit__(self, *args: object) -> None:
            return None

    lock = _Lock()
    result = _Result(value={"ok": True, "result_enc": "x"})
    handle = TaskResultHandle(result, box, lock=lock)
    with pytest.raises(DispatchError):
        handle.result()
    assert lock.entered == 1
    assert result.get_calls == 1
