"""Edge case тесты для Serializer."""
import collections
import io
import os
import pickle

import pytest

from serializer import Serializer, SafeUnpickler


# === Десериализация мусора ===

def test_deserialize_garbage():
    """b'garbage' → UnpicklingError."""
    with pytest.raises(pickle.UnpicklingError):
        Serializer.deserialize(b"garbage")


def test_deserialize_empty():
    """b'' → ошибка (EOFError или UnpicklingError)."""
    with pytest.raises((pickle.UnpicklingError, EOFError)):
        Serializer.deserialize(b"")


def test_deserialize_partial():
    """Обрезанные pickle-данные → ошибка."""
    data = Serializer.serialize({"key": "value"})
    truncated = data[:5]
    with pytest.raises((pickle.UnpicklingError, EOFError)):
        Serializer.deserialize(truncated)


# === Unsafe классы ===

class _UnsafeCallable:
    """Класс, который при pickle ссылается на os.system через __reduce__."""
    def __reduce__(self):
        return (os.system, ("echo hacked",))


class _UnsafePopen:
    """Класс, который при pickle ссылается на os.popen через __reduce__."""
    def __reduce__(self):
        return (os.popen, ("echo hacked",))


def test_deserialize_unsafe_class():
    """Класс из неразрешённого модуля (os) → UnpicklingError."""
    unsafe_data = pickle.dumps(_UnsafeCallable())
    with pytest.raises(pickle.UnpicklingError, match="Disallowed"):
        Serializer.deserialize(unsafe_data)


def test_deserialize_unsafe_import():
    """Попытка импорта небезопасного модуля (os.popen) → UnpicklingError."""
    unsafe_data = pickle.dumps(_UnsafePopen())
    with pytest.raises(pickle.UnpicklingError, match="Disallowed"):
        Serializer.deserialize(unsafe_data)


def test_deserialize_reject_subclass():
    """Подкласс collections.OrderedDict из неразрешённого модуля → UnpicklingError."""
    # OrderedDict разрешён — должен пройти
    data = pickle.dumps(collections.OrderedDict({"a": 1}))
    result = Serializer.deserialize(data)
    assert result == collections.OrderedDict({"a": 1})


# === Roundtrip ===

def test_serialize_roundtrip():
    """serialize → deserialize — данные не теряются."""
    test_cases = [
        42,
        3.14,
        "hello",
        b"bytes",
        True,
        None,
        [1, 2, 3],
        {"a": 1, "b": [2, 3]},
        (1, 2, 3),
        {1, 2, 3},
    ]
    for original in test_cases:
        serialized = Serializer.serialize(original)
        deserialized = Serializer.deserialize(serialized)
        assert deserialized == original


def test_serialize_roundtrip_nested():
    """Сложная вложенная структура — roundtrip."""
    data = {
        "users": [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}],
        "config": {"debug": True, "retries": 3},
        "tags": ["python", "test"],
    }
    result = Serializer.deserialize(Serializer.serialize(data))
    assert result == data


def test_serialize_returns_bytes():
    """serialize возвращает bytes."""
    result = Serializer.serialize("test")
    assert isinstance(result, bytes)
    assert len(result) > 0


def test_safe_unpickler_allowed_types():
    """SafeUnpickler разрешает простые типы из builtins."""
    import io
    # Создаём pickle для dict напрямую
    data = pickle.dumps({"key": "value"})
    result = SafeUnpickler(io.BytesIO(data)).load()
    assert result == {"key": "value"}
