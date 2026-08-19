"""Короткое имя модуля, срез bound self, запрет Application/state в payload."""
from __future__ import annotations

from typing import Any, Callable

from core.dispatch.errors import PAYLOAD_FORBIDDEN, PAYLOAD_NOT_SERIALIZABLE, DispatchError

_FORBIDDEN_TYPE_NAMES = frozenset({"Application"})
_FORBIDDEN_KW = frozenset({"self", "state"})


def short_module_name(fn: Callable[..., Any]) -> str:
    """modules.auth.provider → auth; core.database → db."""
    module = getattr(fn, "__module__", "") or "unknown"
    parts = module.split(".")
    if parts[0] == "modules" and len(parts) > 1:
        return parts[1]
    if parts[0] == "core" and len(parts) > 1:
        return "db" if parts[1] == "database" else parts[1]
    return parts[-1]


def method_name(fn: Callable[..., Any]) -> str:
    return getattr(fn, "_original_name", None) or getattr(fn, "__name__", "unknown")


def strip_bound_self(fn: Callable[..., Any], args: tuple[Any, ...]) -> tuple[Any, ...]:
    """Убрать экземпляр-владелец метода из args — на воркере метод уже bound."""
    if not args:
        return args
    first = args[0]
    owner = getattr(fn, "__qualname__", "").rsplit(".", 1)
    if len(owner) != 2:
        return args
    if type(first).__name__ == owner[0].split(".")[-1]:
        return args[1:]
    return args


def assert_payload_allowed(args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
    """self / state / Application в payload запрещены."""
    for key in kwargs:
        if key in _FORBIDDEN_KW:
            raise DispatchError(PAYLOAD_FORBIDDEN, f"kwarg '{key}' is forbidden")
    for value in args:
        _reject_forbidden(value)
    for value in kwargs.values():
        _reject_forbidden(value)


def require_jsonable(payload: dict[str, Any]) -> bytes:
    """Сериализовать payload или PAYLOAD_NOT_SERIALIZABLE."""
    import json

    try:
        return json.dumps(payload, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise DispatchError(PAYLOAD_NOT_SERIALIZABLE, "payload is not JSON-serializable") from exc


def sanitize(
    fn: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> tuple[str, str, tuple[Any, ...], dict[str, Any]]:
    """Вернуть (module, method, args, kwargs) без self и запрещённых объектов."""
    clean_args = strip_bound_self(fn, args)
    assert_payload_allowed(clean_args, kwargs)
    return short_module_name(fn), method_name(fn), clean_args, kwargs


def _reject_forbidden(value: Any) -> None:
    cls = type(value)
    if cls.__name__ in _FORBIDDEN_TYPE_NAMES:
        raise DispatchError(PAYLOAD_FORBIDDEN, f"{cls.__name__} must not be in payload")
    module = getattr(cls, "__module__", "")
    if cls.__name__ == "Application" and module.startswith("core"):
        raise DispatchError(PAYLOAD_FORBIDDEN, "Application must not be in payload")
