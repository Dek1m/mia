"""Тесты схемы Preferences."""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from modules_system.pref_spec import PrefField, apply_to_config, coerce, group_fields, resolve_value


BOOL_FIELD = PrefField("cache", "Cache", "hint", "bool", True, "Limits", env="X_CACHE")
INT_FIELD = PrefField("size", "Size", "hint", "int", 10, "Limits", minimum=1, maximum=100)
ENUM_FIELD = PrefField("level", "Level", "hint", "enum", "INFO", "Logging", options=("DEBUG", "INFO"))


def test_coerce_bool_and_int() -> None:
    assert coerce(BOOL_FIELD, "true") is True
    assert coerce(BOOL_FIELD, 0) is False
    assert coerce(INT_FIELD, "12") == 12
    with pytest.raises(ValueError):
        coerce(INT_FIELD, 0)
    with pytest.raises(ValueError):
        coerce(ENUM_FIELD, "TRACE")


def test_group_fields_order() -> None:
    extra = PrefField("other", "Other", "h", "string", "", "Network")
    groups = group_fields("auth", (INT_FIELD, extra, BOOL_FIELD), {"auth.size": 3})
    assert [g["id"] for g in groups] == ["Limits", "Network"]
    assert groups[0]["fields"][0]["value"] == 3


def test_resolve_env_overlay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X_CACHE", "false")
    assert resolve_value("mod", BOOL_FIELD) is False
    assert resolve_value("mod", BOOL_FIELD, {"mod.cache": True}) is True


def test_apply_frozen_replace() -> None:
    @dataclass(frozen=True)
    class Cfg:
        level: str = "INFO"

    next_cfg = apply_to_config(Cfg(), ENUM_FIELD, "DEBUG")
    assert next_cfg.level == "DEBUG"
