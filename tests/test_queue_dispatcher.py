"""Unit-тесты QueueDispatcher на моке клиента. Без Redis."""
from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from core.dispatch.codec import MIA_QUEUE, MIA_TASK_NAME
from core.dispatch.errors import PAYLOAD_FORBIDDEN, PAYLOAD_NOT_SERIALIZABLE, DispatchError
from core.dispatch.secret_box import SecretBox
from modules.worker.dispatcher import QueueDispatcher


class _FakeAsyncResult:
    def __init__(self, task_id: str) -> None:
        self.id = task_id

    def get(self, timeout: float | None = None) -> Any:
        return {"ok": True, "result_enc": "unused"}

    def ready(self) -> bool:
        return True

    def cancelled(self) -> bool:
        return False


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def send(
        self,
        name: str,
        *,
        args: tuple = (),
        kwargs: dict | None = None,
        queue: str | None = None,
        **options: Any,
    ) -> _FakeAsyncResult:
        self.calls.append({"name": name, "args": args, "kwargs": kwargs, "queue": queue})
        return _FakeAsyncResult("tid-1")


@pytest.fixture
def box() -> SecretBox:
    return SecretBox(bytes.fromhex("1111111111111111111111111111111111111111111111111111111111111111"))


def test_send_encrypts_payload(box: SecretBox) -> None:
    client = _FakeClient()
    dp = QueueDispatcher(client, box)

    def add(a: int, b: int) -> int:
        return a + b

    dp.dispatch_async(add, 1, 2)
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["name"] == MIA_TASK_NAME
    assert call["queue"] == MIA_QUEUE
    envelope = call["args"][0]
    assert envelope["module"]
    assert envelope["method"] == "add"
    assert "payload_enc" in envelope
    assert "args" not in envelope
    payload = box.decrypt(envelope["payload_enc"])
    assert b'"args"' in payload
    assert b"1" in payload


def test_strips_bound_self(box: SecretBox) -> None:
    client = _FakeClient()
    dp = QueueDispatcher(client, box)

    class AuthProvider:
        def login(self, user: str) -> str:
            return user

    provider = AuthProvider()
    dp.dispatch_async(provider.login, "alice")
    envelope = client.calls[0]["args"][0]
    raw = box.decrypt(envelope["payload_enc"]).decode()
    assert "alice" in raw
    assert "AuthProvider" not in raw


def test_forbids_application(box: SecretBox) -> None:
    client = _FakeClient()
    dp = QueueDispatcher(client, box)

    class Application:
        pass

    def use(app: Application) -> None:
        return None

    with pytest.raises(DispatchError) as exc:
        dp.dispatch_async(use, Application())
    assert exc.value.code == PAYLOAD_FORBIDDEN


def test_application_default_is_queue(monkeypatch: pytest.MonkeyPatch, box: SecretBox) -> None:
    monkeypatch.delenv("MIA_DISPATCH", raising=False)
    monkeypatch.setenv("MIA_TASK_CRYPTO_KEY", "11" * 32)
    from core.application import Application
    from core.dispatch.local import LocalInvokeDispatcher
    from modules.worker.dispatcher import QueueDispatcher as QD

    class _Client:
        def send(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("no send")

    monkeypatch.setattr("modules.worker.dispatcher.QueueDispatcher.from_config", lambda: QD(_Client(), box))
    app = Application(modules_dir="modules")
    assert isinstance(app.smart_dispatcher, QD)
    assert not isinstance(app.smart_dispatcher, LocalInvokeDispatcher)


def test_application_local_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIA_DISPATCH", "local")
    from core.application import Application
    from core.dispatch.local import LocalInvokeDispatcher

    app = Application(modules_dir="modules")
    assert isinstance(app.smart_dispatcher, LocalInvokeDispatcher)


def test_not_serializable(box: SecretBox) -> None:
    client = _FakeClient()
    dp = QueueDispatcher(client, box)

    def take(obj: object) -> None:
        return None

    with pytest.raises(DispatchError) as exc:
        dp.dispatch_async(take, object())
    assert exc.value.code == PAYLOAD_NOT_SERIALIZABLE


def test_send_and_get_share_rlock(box: SecretBox) -> None:
    dp = QueueDispatcher(_FakeClient(), box)

    def ping() -> None:
        return None

    handle = dp.dispatch_async(ping)
    assert dp._write_lock is handle._lock
    assert type(dp._write_lock).__name__ == "RLock"


def test_send_serializes_parallel_calls(box: SecretBox) -> None:
    class _SlowClient:
        def __init__(self) -> None:
            self.inside = 0
            self.max_inside = 0
            self._guard = threading.Lock()

        def send(self, *args: Any, **kwargs: Any) -> _FakeAsyncResult:
            with self._guard:
                self.inside += 1
                self.max_inside = max(self.max_inside, self.inside)
            time.sleep(0.05)
            with self._guard:
                self.inside -= 1
            return _FakeAsyncResult("tid")

    client = _SlowClient()
    dp = QueueDispatcher(client, box)

    def ping() -> None:
        return None

    threads = [threading.Thread(target=lambda: dp.dispatch_async(ping)) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert client.max_inside == 1


def test_send_failure_propagates(box: SecretBox) -> None:
    class _Boom:
        def send(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("redis down")

    dp = QueueDispatcher(_Boom(), box)

    def ping() -> None:
        return None

    with pytest.raises(RuntimeError, match="redis down"):
        dp.dispatch_async(ping)
