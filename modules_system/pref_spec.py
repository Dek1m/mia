"""Схема настроек модуля для Preferences.

Модуль объявляет SETTINGS на конфиге. System собирает каталог,
раскладывает по логическим блокам (group), отдаёт albedo.
Значение: defaults → ENV → system.pref. Секреты в каталог не входят.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, fields, is_dataclass, replace
from typing import Any, Iterable, Literal, Sequence

PrefKind = Literal["bool", "int", "float", "string", "enum"]
PrefTarget = Literal["runtime", "env", "compose"]

_TRUE = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True)
class PrefField:
    """Одно настраиваемое поле модуля."""

    key: str
    label: str
    hint: str
    kind: PrefKind
    default: Any
    group: str
    env: str | None = None
    target: PrefTarget = "runtime"
    needs_restart: bool = False
    minimum: float | None = None
    maximum: float | None = None
    options: tuple[str, ...] = ()

    def qualified(self, module: str) -> str:
        return f"{module}.{self.key}"


def parse_env(field: PrefField, raw: str) -> Any:
    """Привести строку ENV к типу поля."""
    text = raw.strip()
    if field.kind == "bool":
        return text.lower() in _TRUE
    if field.kind == "int":
        return int(text)
    if field.kind == "float":
        return float(text)
    return text


def coerce(field: PrefField, raw: Any) -> Any:
    """Привести JSON/UI-значение к типу поля. ValueError если мусор."""
    if field.kind == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            return raw.strip().lower() in _TRUE
        if isinstance(raw, (int, float)) and raw in (0, 1):
            return bool(raw)
        raise ValueError(f"{field.key}: expected bool")
    if field.kind == "int":
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            raise ValueError(f"{field.key}: expected int")
        value = int(raw)
        _check_range(field, value)
        return value
    if field.kind == "float":
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            raise ValueError(f"{field.key}: expected float")
        value = float(raw)
        _check_range(field, value)
        return value
    if field.kind == "enum":
        text = str(raw)
        if field.options and text not in field.options:
            raise ValueError(f"{field.key}: expected one of {field.options}")
        return text
    if raw is None:
        return ""
    return str(raw)


def _check_range(field: PrefField, value: float) -> None:
    if field.minimum is not None and value < field.minimum:
        raise ValueError(f"{field.key}: min {field.minimum}")
    if field.maximum is not None and value > field.maximum:
        raise ValueError(f"{field.key}: max {field.maximum}")


def resolve_value(module: str, field: PrefField, overlay: dict[str, Any] | None = None) -> Any:
    """Каскад: overlay (system.pref) → ENV → default."""
    qualified = field.qualified(module)
    if overlay and qualified in overlay:
        return coerce(field, overlay[qualified])
    if field.env:
        raw = os.getenv(field.env)
        if raw is not None and raw.strip() != "":
            try:
                return parse_env(field, raw)
            except ValueError:
                pass
    return field.default


def unwrap_stored(value: Any) -> Any:
    """JSONB мог быть скаляром или {\"v\": ...}."""
    if isinstance(value, dict) and set(value.keys()) == {"v"}:
        return value["v"]
    return value


def field_dto(module: str, field: PrefField, value: Any) -> dict[str, Any]:
    """Публичный JSON поля. Секретов нет."""
    dto: dict[str, Any] = {
        "key": field.qualified(module),
        "name": field.key,
        "label": field.label,
        "hint": field.hint,
        "kind": field.kind,
        "value": value,
        "default": field.default,
        "group": field.group,
        "env": field.env,
        "target": field.target,
        "needs_restart": field.needs_restart or field.target in ("env", "compose"),
    }
    if field.minimum is not None:
        dto["min"] = field.minimum
    if field.maximum is not None:
        dto["max"] = field.maximum
    if field.options:
        dto["options"] = list(field.options)
    return dto


def group_fields(module: str, fields: Sequence[PrefField], values: dict[str, Any]) -> list[dict[str, Any]]:
    """Раскладка по логическим блокам, порядок первого появления group."""
    order: list[str] = []
    buckets: dict[str, list[dict[str, Any]]] = {}
    for field in fields:
        if field.group not in buckets:
            order.append(field.group)
            buckets[field.group] = []
        qualified = field.qualified(module)
        current = values[qualified] if qualified in values else field.default
        buckets[field.group].append(field_dto(module, field, current))
    return [{"id": name, "label": name, "fields": buckets[name]} for name in order]


def apply_to_config(config: Any, field: PrefField, value: Any) -> Any:
    """Записать поле в dataclass. Frozen → новый экземпляр. Иначе setattr."""
    coerced = coerce(field, value)
    if not is_dataclass(config) or not hasattr(config, field.key):
        return config
    params = getattr(config, "__dataclass_params__", None)
    if params is not None and params.frozen:
        return replace(config, **{field.key: coerced})
    setattr(config, field.key, coerced)
    return config


def live_values(module: str, config: Any, fields: Iterable[PrefField]) -> dict[str, Any]:
    """Текущие значения с живого конфига."""
    out: dict[str, Any] = {}
    for field in fields:
        if config is not None and hasattr(config, field.key):
            out[field.qualified(module)] = getattr(config, field.key)
        else:
            out[field.qualified(module)] = field.default
    return out


def config_field_names(config: Any) -> set[str]:
    if config is None or not is_dataclass(config):
        return set()
    return {item.name for item in fields(config)}
