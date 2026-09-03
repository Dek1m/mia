"""ADR-005: ModuleRuntimeRegistry — HASH+hb, unknown без hb, redis down."""
from __future__ import annotations

from typing import Any

from core.application import Application
from core.dispatch.local import LocalInvokeDispatcher
from modules_system.runtime_registry import (
    HASH_PREFIX,
    HB_TTL_SECONDS,
    ModuleRuntimeRegistry,
    merge_runtime_views,
)
from modules_system.verification import VerificationMode


class FakeRedis:
    """In-memory HASH + STRING с TTL-флагом (без реального истечения)."""

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.strings: dict[str, str] = {}
        self.ttl: dict[str, int] = {}
        self.fail = False

    def _guard(self) -> None:
        if self.fail:
            raise ConnectionError("redis down")

    def hset(self, key: str, field: str | None = None, value: str | None = None, mapping: dict | None = None) -> int:
        self._guard()
        bucket = self.hashes.setdefault(key, {})
        if mapping:
            bucket.update(mapping)
            return len(mapping)
        if field is not None and value is not None:
            bucket[field] = value
            return 1
        return 0

    def hgetall(self, key: str) -> dict[str, str]:
        self._guard()
        return dict(self.hashes.get(key, {}))

    def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self._guard()
        self.strings[key] = value
        if ex is not None:
            self.ttl[key] = ex
        return True

    def exists(self, key: str) -> int:
        self._guard()
        return 1 if key in self.strings else 0

    def expire_hb(self, service: str) -> None:
        self.strings.pop(f"{HASH_PREFIX}{service}:hb", None)


def _snap(name: str, service: str = "belle") -> dict[str, Any]:
    return {
        "name": name,
        "display_name": name.title(),
        "version": "1.0.0",
        "status": "loaded",
        "health": "ok",
        "load_on": "all",
        "is_system": True,
        "is_example": False,
        "source": "image",
        "error": None,
        "pid": 1,
        "service": service,
        "updated_at": "2026-09-02T12:00:00Z",
    }


class TestModuleRuntimeRegistry:
    def test_hash_and_hb_schema(self) -> None:
        redis = FakeRedis()
        registry = ModuleRuntimeRegistry("belle", client=redis)
        registry.publish_all([_snap("auth"), _snap("db")])
        hash_key = f"{HASH_PREFIX}belle"
        hb_key = f"{HASH_PREFIX}belle:hb"
        assert hash_key in redis.hashes
        assert "auth" in redis.hashes[hash_key]
        assert "db" in redis.hashes[hash_key]
        assert redis.strings[hb_key] == "1"
        assert redis.ttl[hb_key] == HB_TTL_SECONDS
        snapshots, alive = registry.read_service("belle")
        assert alive is True
        assert snapshots["auth"]["status"] == "loaded"

    def test_unknown_without_heartbeat(self) -> None:
        redis = FakeRedis()
        registry = ModuleRuntimeRegistry("belle", client=redis)
        registry.publish_all([_snap("auth")])
        redis.expire_hb("belle")
        viewed = registry.view_service("belle")
        assert viewed["auth"]["status"] == "unknown"
        assert viewed["auth"]["health"] == "unknown"
        assert viewed["auth"]["pid"] is None
        assert f"{HASH_PREFIX}belle" in redis.hashes

    def test_upsert_one_field(self) -> None:
        redis = FakeRedis()
        registry = ModuleRuntimeRegistry("belle-worker", client=redis)
        registry.upsert(_snap("fs", "belle-worker"))
        snapshots, alive = registry.read_service("belle-worker")
        assert snapshots["fs"]["name"] == "fs"
        assert alive is False

    def test_redis_down_does_not_raise(self) -> None:
        redis = FakeRedis()
        redis.fail = True
        registry = ModuleRuntimeRegistry("belle", client=redis)
        registry.upsert(_snap("auth"))
        registry.publish_all([_snap("auth")])
        registry.heartbeat()
        snapshots, alive = registry.read_service("belle")
        assert snapshots == {}
        assert alive is False


class TestMergeRuntimeViews:
    def test_union_and_unknown_other_service(self) -> None:
        belle = {"auth": _snap("auth"), "db": _snap("db")}
        worker = {"auth": _snap("auth", "belle-worker")}
        items = {item["name"]: item for item in merge_runtime_views(belle, worker)}
        assert set(items) == {"auth", "db"}
        assert items["auth"]["services"]["belle"]["status"] == "loaded"
        assert items["auth"]["services"]["worker"]["status"] == "loaded"
        assert items["db"]["services"]["worker"]["status"] == "unknown"


class TestLoadSurvivesRedisDown:
    def test_load_all_with_dead_registry(self, tmp_path) -> None:
        mod_dir = tmp_path / "core"
        mod_dir.mkdir()
        (mod_dir / "__init__.py").write_text(
            "from modules_system.module_base import ModuleBase, ModuleMeta\n"
            "class CoreModule(ModuleBase):\n"
            "    @property\n"
            "    def name(self) -> str:\n"
            '        return "core"\n'
            "    @property\n"
            "    def meta(self) -> ModuleMeta:\n"
            "        return ModuleMeta(is_example=False)\n"
            "    def on_load(self, state):\n"
            "        pass\n",
            encoding="utf-8",
        )
        redis = FakeRedis()
        redis.fail = True
        app = Application(
            modules_dir=str(tmp_path),
            verification_mode=VerificationMode.DISABLED,
            dispatcher=LocalInvokeDispatcher(),
        )
        app.set_runtime_registry(ModuleRuntimeRegistry("belle", client=redis))
        app.load_all_modules(role="api")
        assert "core" in app.modules.list_all()
        app.shutdown()
