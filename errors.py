"""Кастомные исключения для MIA."""
from __future__ import annotations


class MiaError(Exception):
    """Базовое исключение MIA."""


class ModuleError(MiaError):
    """Ошибка связанная с модулем."""


class ModuleNotFoundError(ModuleError):
    """Модуль не найден."""


class ModuleLoadError(ModuleError):
    """Ошибка загрузки модуля."""


class ProcessPoolError(MiaError):
    """Ошибка пула процессов."""


class CircuitOpenError(MiaError):
    """Circuit breaker открыт."""


class ShutdownTimeoutError(MiaError):
    """Таймаут при завершении."""
