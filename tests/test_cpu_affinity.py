"""Unit-тесты для CpuAffinityProvider."""
import os
import platform

import pytest

from cpu_affinity import CpuAffinityProvider


@pytest.fixture
def affinity():
    """Создаёт CpuAffinityProvider для тестов."""
    return CpuAffinityProvider()


# === Базовые тесты ===

def test_cpu_affinity_creation():
    """CpuAffinityProvider() создаётся без ошибок."""
    cap = CpuAffinityProvider()
    assert cap is not None
    assert isinstance(cap, CpuAffinityProvider)


def test_get_cpu_count(affinity):
    """get_cpu_count() возвращает > 0."""
    count = affinity.get_cpu_count()
    assert count > 0
    assert isinstance(count, int)


# === set_affinity ===

def test_set_affinity_linux(affinity):
    """set_affinity(0, {0}) на Linux — не падает."""
    if platform.system() != "Linux":
        pytest.skip("Только Linux")
    # pid=0 — текущий процесс, cores={0} — первое ядро
    result = affinity.set_affinity(0, {0})
    assert isinstance(result, bool)


def test_set_affinity_returns_bool(affinity):
    """set_affinity возвращает bool."""
    result = affinity.set_affinity(0, {0})
    assert isinstance(result, bool)
    # На Linux должно быть True (если ядро 0 доступно)
    if platform.system() == "Linux":
        assert result is True


def test_set_affinity_invalid_core(affinity):
    """set_affinity с несуществующим ядром — возвращает False."""
    result = affinity.set_affinity(0, {99999})
    assert result is False


# === get_affinity ===

def test_get_affinity(affinity):
    """get_affinity(0) возвращает set (или None на неподдерживаемых платформах)."""
    result = affinity.get_affinity(0)
    if platform.system() == "Linux":
        assert isinstance(result, set)
        assert len(result) > 0
    else:
        # На Windows без psutil может вернуть None
        assert result is None or isinstance(result, set)


def test_get_affinity_after_set(affinity):
    """После set_affinity — get_affinity возвращает обновлённое значение."""
    if platform.system() != "Linux":
        pytest.skip("Только Linux")
    affinity.set_affinity(0, {0})
    result = affinity.get_affinity(0)
    assert isinstance(result, set)
    assert 0 in result
