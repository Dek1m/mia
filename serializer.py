"""Serializer — безопасная сериализация данных."""
from __future__ import annotations

import io
import pickle
from typing import Any

from argenta_logging import get_logger

log = get_logger(__name__)


class SafeUnpickler(pickle.Unpickler):
    """Безопасный unpickler — разрешает только простые типы."""

    SAFE_MODULES: dict[str, set[str]] = {
        "builtins": {
            "dict", "list", "set", "tuple", "int", "float",
            "str", "bytes", "NoneType", "bool",
        },
        "collections": {"OrderedDict", "defaultdict"},
    }

    def find_class(self, module: str, name: str) -> Any:
        if module in self.SAFE_MODULES and name in self.SAFE_MODULES[module]:
            return super().find_class(module, name)
        raise pickle.UnpicklingError(f"Disallowed: {module}.{name}")


class Serializer:
    """Безопасный сериализатор на основе pickle."""

    @staticmethod
    def serialize(data: Any) -> bytes:
        """Сериализовать данные в bytes."""
        return pickle.dumps(data)

    @staticmethod
    def deserialize(data: bytes) -> Any:
        """Безопасно десериализовать данные из bytes."""
        return SafeUnpickler(io.BytesIO(data)).load()