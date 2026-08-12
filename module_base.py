"""Базовый класс для модулей."""
from abc import ABC, abstractmethod
from typing import Any, Callable
import functools

from argenta_logging import get_logger

log = get_logger(__name__)


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

    def on_load(self, state: "Application") -> None:  # noqa: F821
        """Вызывается при загрузке модуля. Инициализация.

        Args:
            state: Экземпляр Application для доступа к другим модулям и API.
        """
        pass

    def on_unload(self) -> None:
        """Вызывается при выгрузке модуля. Очистка ресурсов."""
        pass