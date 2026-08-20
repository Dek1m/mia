"""Задача mia.run для Celery-воркера. Это не процесс."""
from __future__ import annotations

import os
from typing import Any

from celery import shared_task
from celery.signals import worker_process_init

from argenta_logging import get_logger

from core.dispatch.envelope import TaskRequest, TaskResult
from core.dispatch.errors import DispatchError, TASK_FAILED, WORKER_NOT_READY
from core.dispatch.registry import TaskTargetRegistry
from core.dispatch.secret_box import SecretBox
from core.dispatch.codec import decode_payload, encode_result

log = get_logger(__name__)

_box: SecretBox | None = None
_registry: TaskTargetRegistry | None = None


@worker_process_init.connect
def _on_worker_process_init(**_kwargs: Any) -> None:
    """SecretBox + Application(LocalInvoke) + реестр методов."""
    global _box, _registry
    from core.application import Application
    from core.dispatch.local import LocalInvokeDispatcher

    log.info("worker_process_init", extra={"pid": os.getpid()})
    _box = SecretBox.from_env()
    allowed = [
        m.strip()
        for m in os.environ.get("MIA_WORKER_MODULES", "db,auth").split(",")
        if m.strip()
    ]
    log.info("worker_loading_modules", extra={"allowed": allowed})
    app = Application(dispatcher=LocalInvokeDispatcher(), allowed_modules=allowed)
    app.load_all_modules()
    loaded = app.modules.list_all()
    log.info("worker_modules_loaded", extra={"modules": loaded, "count": len(loaded)})
    registry = TaskTargetRegistry()
    for name in loaded:
        module = app.modules.get(name)
        if module is not None:
            registry.register_object(name, module)
            provider = getattr(module, "_provider", None)
            if provider is not None:
                registry.register_object(name, provider)
                log.debug("worker_provider_registered", extra={"module_name": name})
    registry.register_database(app.database)
    _registry = registry
    log.info("mia_worker_ready", extra={"pid": os.getpid(), "modules": loaded})


def mia_run(envelope: dict[str, Any]) -> dict[str, Any]:
    """Тело задачи. Всегда result-dict, без Celery FAILURE."""
    if _box is None or _registry is None:
        log.error("mia_run_not_ready", extra={"pid": os.getpid()})
        return TaskResult.fail(WORKER_NOT_READY, "worker is not ready").to_dict()
    try:
        request = TaskRequest.from_dict(envelope)
        log.info(
            "mia_run_start",
            extra={
                "task_id": request.id,
                "task_module": request.module,
                "task_method": request.method,
                "timeout": request.timeout,
            },
        )
        payload = decode_payload(_box, request.payload_enc)
        args = payload.get("args") or []
        kwargs = payload.get("kwargs") or {}
        if not isinstance(args, list) or not isinstance(kwargs, dict):
            raise DispatchError("PAYLOAD_NOT_SERIALIZABLE", "payload must be an object")
        target = _registry.get(request.module, request.method)
        log.debug(
            "mia_run_invoke",
            extra={"task_id": request.id, "task_module": request.module, "task_method": request.method},
        )
        value = target(*args, **kwargs)
        log.info("mia_run_ok", extra={"task_id": request.id, "task_module": request.module, "task_method": request.method})
        return TaskResult.ok_enc(encode_result(_box, value)).to_dict()
    except DispatchError as exc:
        log.error(
            "mia_run_failed",
            extra={"code": exc.code, "task_module": envelope.get("module"), "task_method": envelope.get("method")},
        )
        return TaskResult.fail(exc.code, exc.message, type(exc).__name__).to_dict()
    except Exception:
        log.error(
            "mia_run_failed",
            extra={"code": TASK_FAILED, "task_module": envelope.get("module"), "task_method": envelope.get("method")},
        )
        return TaskResult.fail(TASK_FAILED, "task failed").to_dict()


# Регистрация имени mia.run на celery app. mia_run остаётся обычной функцией.
shared_task(name="mia.run")(mia_run)
