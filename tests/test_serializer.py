"""Unit-тесты для Serializer."""
import pytest

from serializer import Serializer


# === Базовые тесты ===

def test_serialize_deserialize():
    """serialize/deserialize простых данных — roundtrip."""
    data = {"key": "value", "count": 42}
    serialized = Serializer.serialize(data)
    deserialized = Serializer.deserialize(serialized)
    assert deserialized == data


def test_serialize_complex_object():
    """serialize/deserialize сложных объектов (dict, list, вложенные)."""
    data = {
        "users": [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}],
        "config": {"debug": True, "retries": 3},
        "tags": ["python", "multiprocessing"],
        "nested": {"level": {"deep": {"value": [1, 2, 3]}}},
    }
    serialized = Serializer.serialize(data)
    deserialized = Serializer.deserialize(serialized)
    assert deserialized == data
    assert deserialized["users"][0]["name"] == "Alice"
    assert deserialized["nested"]["level"]["deep"]["value"] == [1, 2, 3]


def test_serialize_bytes():
    """serialize возвращает bytes."""
    data = "hello world"
    result = Serializer.serialize(data)
    assert isinstance(result, bytes)
    assert len(result) > 0


def test_serialize_none():
    """serialize/deserialize None."""
    result = Serializer.deserialize(Serializer.serialize(None))
    assert result is None


def test_serialize_empty_dict():
    """serialize/deserialize пустого dict."""
    result = Serializer.deserialize(Serializer.serialize({}))
    assert result == {}


def test_serialize_empty_list():
    """serialize/deserialize пустого list."""
    result = Serializer.deserialize(Serializer.serialize([]))
    assert result == []


def test_serialize_string():
    """serialize/deserialize строки."""
    data = "тестовая строка на русском"
    result = Serializer.deserialize(Serializer.serialize(data))
    assert result == data


def test_serialize_number():
    """serialize/deserialize числа."""
    for val in [0, -1, 3.14, 10**100]:
        result = Serializer.deserialize(Serializer.serialize(val))
        assert result == val


def test_serialize_tuple():
    """serialize/deserialize tuple."""
    data = (1, 2, 3)
    result = Serializer.deserialize(Serializer.serialize(data))
    assert result == data
    assert isinstance(result, tuple)


def test_serialize_set():
    """serialize/deserialize set."""
    data = {1, 2, 3}
    result = Serializer.deserialize(Serializer.serialize(data))
    assert result == data
    assert isinstance(result, set)
