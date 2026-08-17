"""Базовый класс для модулей и метаданные модуля."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable
import functools

from argenta_logging import get_logger

log = get_logger(__name__)


@dataclass
class ModuleMeta:
    """Конфигурация модуля: permissions, cache, lock, timeout.

    Описывает поведение методов модуля на уровне метаданных:
    - permissions: какие разрешения нужны для вызова метода
    - cache_rules: TTL кеширования результатов метода
    - lock_rules: шаблоны блокировок (например, по пользователю)
    - timeout_defaults: таймауты по умолчанию для методов
    """

    permissions: dict[str, str] = field(default_factory=dict)
    cache_rules: dict[str, int] = field(default_factory=dict)
    lock_rules: dict[str, str] = field(default_factory=dict)
    timeout_defaults: dict[str, float] = field(default_factory=dict)


def api_method(fn: Callable | None = None, *, parallel: bool = False) -> Callable:
    """Декоратор для регистрации API метода.

    Args:
        fn: Декорируемая функция (если используется без аргументов).
        parallel: Если True, метод будет выполняться в отдельном потоке.

    Returns:
        Декорированная функция с атрибутами _is_api_method и _parallel.
    """

    def decorator(func: Callable) -> Callable:
        func._is_api_method = True  # type: ignore[attr-defined]
        func._parallel = parallel  # type: ignore[attr-defined]

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return func(*args, **kwargs)

        wrapper._is_api_method = True  # type: ignore[attr-defined]
        wrapper._parallel = parallel  # type: ignore[attr-defined]
        return wrapper

    if fn is not None:
        return decorator(fn)
    return decorator


class ModuleBase(ABC):
    """Базовый класс для всех модулей.

    Модули должны наследоваться от этого класса и реализовывать
    абстрактное свойство name. Методы on_load/on_unload вызываются
    при загрузке/выгрузке модуля.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Уникальное имя модуля."""
        ...

    @property
    def version(self) -> str:
        """Версия модуля по умолчанию."""
        return "0.0.0"

    @property
    def meta(self) -> ModuleMeta:
        """Метаданные модуля: permissions, cache, lock, timeout.

        Переопределяйте в наследниках для декларативного описания
        поведения методов модуля.
        """
        return ModuleMeta()

    def on_load(self, state: "Application") -> None:  # noqa: F821
        """Вызывается при загрузке модуля. Инициализация.

        Args:
            state: Экземпляр Application для доступа к другим модулям и API.
        """
        pass

    def on_unload(self) -> None:
        """Вызывается при выгрузке модуля. Очистка ресурсов."""
        pass