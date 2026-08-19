"""ShaltirDispatcher: sanitize → encrypt → send mia.run."""
from __future__ import annotations

import json
import threading
from typing import Any

from argenta_logging import get_logger

from core.dispatch.envelope import TaskRequest
from core.dispatch.errors import DispatchError
from core.dispatch.handle import ShaltirResultHandle
from core.dispatch.parse import parse_dispatch_args
from core.dispatch.sanitize import require_jsonable, sanitize
from core.dispatch.secret_box import SecretBox
from core.interfaces import ISmartDispatcher

log = get_logger(__name__)

MIA_TASK_NAME = "mia.run"
MIA_QUEUE = "mia"


class ShaltirDispatcher(ISmartDispatcher):
    """Клиентский диспетчер: одна очередь mia, payload только в ciphertext."""

    def __init__(self, client: Any, box: SecretBox) -> None:
        self._client = client
        self._box = box
        self._write_lock = threading.Lock()

    def dispatch(self, first: Any, *args: Any, **kwargs: Any) -> Any:
        return self.dispatch_async(first, *args, **kwargs).result()

    def dispatch_async(self, first: Any, *args: Any, **kwargs: Any) -> ShaltirResultHandle:
        task, fn, call_args = parse_dispatch_args(first, args)
        module, method, clean_args, clean_kwargs = sanitize(fn, call_args, kwargs)
        timeout = float(getattr(fn, "_task_timeout", 10.0))
        payload = require_jsonable({"args": list(clean_args), "kwargs": clean_kwargs})
        envelope = TaskRequest(
            id=str(task.id),
            module=module,
            method=method,
            task_type=getattr(task.task_type, "value", str(task.task_type)),
            timeout=timeout,
            payload_enc=self._box.encrypt(payload),
        )
        log.info(
            "task_sent",
            extra={
                "task_id": envelope.id,
                "task_module": module,
                "task_method": method,
                "queue": MIA_QUEUE,
            },
        )
        async_result = self._client.send(
            MIA_TASK_NAME,
            args=(envelope.to_dict(),),
            queue=MIA_QUEUE,
        )
        return ShaltirResultHandle(async_result, self._box, timeout=timeout)

    def acquire_lock(self) -> None:
        self._write_lock.acquire()

    def release_lock(self) -> None:
        self._write_lock.release()


def encode_result(box: SecretBox, value: Any) -> str:
    """Зашифровать результат. Не логировать содержимое."""
    return box.encrypt(json.dumps(value, separators=(",", ":")).encode("utf-8"))


def decode_payload(box: SecretBox, payload_enc: str) -> dict[str, Any]:
    raw = json.loads(box.decrypt(payload_enc))
    if not isinstance(raw, dict):
        raise DispatchError("PAYLOAD_NOT_SERIALIZABLE", "payload must be an object")
    return raw
