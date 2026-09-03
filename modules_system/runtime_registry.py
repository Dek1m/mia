"""ModuleRuntimeRegistry — Redis HASH runtime-снимков модулей (ADR-005)."""
from __future__ import annotations

import json
import os
import threading
from typing import Any, Callable, TypeVar

from argenta_logging import get_logger

log = get_logger(__name__)

T = TypeVar("T")

HASH_PREFIX = "mia:modules:"
HB_TTL_SECONDS = 30
HB_INTERVAL_SECONDS = 10.0
_UNKNOWN = {"status": "unknown", "health": "unknown", "pid": None, "error": None}


def _hash_key(service: str) -> str:
    return f"{HASH_PREFIX}{service}"


def _hb_key(service: str) -> str:
    return f"{HASH_PREFIX}{service}:hb"


def _connect() -> Any:
    import redis

    host = os.environ.get("REDIS_HOST") or os.environ.get("WORKER_REDIS_HOST") or "127.0.0.1"
    port = int(os.environ.get("REDIS_PORT") or os.environ.get("WORKER_REDIS_PORT") or 6379)
    return redis.Redis(host=host, port=port, decode_responses=True)


class ModuleRuntimeRegistry:
    """HASH mia:modules:{service} + STRING hb EX 30. Redis down — не падать."""

    def __init__(self, service: str, client: Any | None = None) -> None:
        self._service = service
        self._client = client
        self._lock = threading.Lock()
        self._hb_stop = threading.Event()
        self._hb_thread: threading.Thread | None = None

    @property
    def service(self) -> str:
        return self._service

    @classmethod
    def from_env(cls, service: str | None = None) -> ModuleRuntimeRegistry:
        name = service or os.environ.get("SERVICE_NAME", "").strip() or "belle"
        return cls(service=name)

    def upsert(self, snapshot: dict[str, Any]) -> None:
        """HSET одного field после load/fail/unload."""
        name = snapshot.get("name")
        if not name:
            return
        payload = json.dumps(snapshot, ensure_ascii=False)
        self._try(lambda c: c.hset(_hash_key(self._service), name, payload))

    def publish_all(self, snapshots: list[dict[str, Any]]) -> None:
        """HSET всех fields + heartbeat. Отфильтрованные модули не кладём."""
        mapping = {
            str(item["name"]): json.dumps(item, ensure_ascii=False)
            for item in snapshots
            if item.get("name")
        }
        if mapping:
            self._try(lambda c: c.hset(_hash_key(self._service), mapping=mapping))
        self.heartbeat()

    def heartbeat(self) -> None:
        self._try(lambda c: c.set(_hb_key(self._service), "1", ex=HB_TTL_SECONDS))

    def start_heartbeat_loop(self, interval: float = HB_INTERVAL_SECONDS) -> None:
        if self._hb_thread is not None:
            return

        def _loop() -> None:
            while not self._hb_stop.wait(interval):
                self.heartbeat()

        self.heartbeat()
        thread = threading.Thread(target=_loop, name="module-runtime-hb", daemon=True)
        self._hb_thread = thread
        thread.start()

    def stop_heartbeat_loop(self) -> None:
        self._hb_stop.set()
        self._hb_thread = None

    def read_service(self, service: str) -> tuple[dict[str, dict[str, Any]], bool]:
        """HGETALL + hb. alive=False если ключа hb нет (протух или не писали)."""
        raw = self._try(lambda c: c.hgetall(_hash_key(service)), default={}) or {}
        alive = bool(self._try(lambda c: c.exists(_hb_key(service)), default=0))
        parsed: dict[str, dict[str, Any]] = {}
        for name, value in raw.items():
            if isinstance(value, dict):
                parsed[str(name)] = value
                continue
            try:
                parsed[str(name)] = json.loads(value)
            except (TypeError, json.JSONDecodeError):
                parsed[str(name)] = {"name": str(name), "status": "failed", "error": "invalid json"}
        return parsed, alive

    def view_service(self, service: str) -> dict[str, dict[str, Any]]:
        """Снимок для UI: нет hb → status/health unknown, HASH не удаляем."""
        snapshots, alive = self.read_service(service)
        if alive:
            return snapshots
        viewed: dict[str, dict[str, Any]] = {}
        for name, item in snapshots.items():
            row = dict(item)
            row.update(_UNKNOWN)
            viewed[name] = row
        return viewed

    def _client_or_connect(self) -> Any:
        if self._client is not None:
            return self._client
        with self._lock:
            if self._client is None:
                self._client = _connect()
            return self._client

    def _try(self, fn: Callable[[Any], T], default: T | None = None) -> T | None:
        try:
            return fn(self._client_or_connect())
        except Exception as exc:
            log.warning("runtime_registry_redis_down", extra={"service": self._service, "error": str(exc)})
            return default


def service_slice(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        return dict(_UNKNOWN)
    return {
        "status": row.get("status") or "unknown",
        "health": row.get("health") or "unknown",
        "pid": row.get("pid"),
        "error": row.get("error"),
    }


def _pick_status(belle: dict[str, Any], worker: dict[str, Any]) -> str:
    for value in (belle.get("status"), worker.get("status")):
        if value == "loaded":
            return "loaded"
    for value in (belle.get("status"), worker.get("status")):
        if value == "failed":
            return "failed"
    return "unknown"


def _pick_health(belle: dict[str, Any], worker: dict[str, Any]) -> str:
    for value in (belle.get("health"), worker.get("health")):
        if value == "ok":
            return "ok"
    for value in (belle.get("health"), worker.get("health")):
        if value == "degraded":
            return "degraded"
    return "unknown"


def merge_runtime_views(
    belle: dict[str, dict[str, Any]],
    worker: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Склейка двух HASH по name. Ключи UI: services.belle / services.worker."""
    items: list[dict[str, Any]] = []
    for name in sorted(set(belle) | set(worker)):
        src = belle.get(name) or worker.get(name) or {}
        belle_svc = service_slice(belle.get(name))
        worker_svc = service_slice(worker.get(name))
        items.append({
            "name": name,
            "display_name": src.get("display_name") or name,
            "version": src.get("version") or "0.0.0",
            "status": _pick_status(belle_svc, worker_svc),
            "health": _pick_health(belle_svc, worker_svc),
            "is_system": bool(src.get("is_system")),
            "source": src.get("source") or "image",
            "services": {"belle": belle_svc, "worker": worker_svc},
        })
    return items
