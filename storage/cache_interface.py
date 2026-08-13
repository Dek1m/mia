"""Cache Interface — Port для реализации кеш-адаптеров.

Mia определяет ИНТЕРФЕЙС. Конкретная реализация (Redis, Memory, File)
— это модуль, который пользователь подключает через on_load().

Пример использования модулем:
    class MyCacheModule(ModuleBase):
        def on_load(self, state):
            state.set_cache(MyRedisCache(host="localhost"))

Пример использования в модуле:
    class PdfModule(ModuleBase):
        def on_load(self, state):
            self._cache = state.cache  # ICache или NullCache
        def process(self, data):
            cached = self._cache.get(f"pdf:{data['id']}")
            if cached:
                return cached
            result = self._heavy_processing(data)
            self._cache.set(f"pdf:{data['id']}", result, ttl=3600)
            return result
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class ICache(ABC):
    """Абстрактный интерфейс кеша.

    Port в терминах Hexagonal Architecture.
    Любая реализация кеша должна наследоваться от этого класса.
    """

    @abstractmethod
    def get(self, key: str) -> Any | None:
        """Получить значение по ключу.

        Args:
            key: Уникальный ключ кеша.

        Returns:
            Значение или None если ключ не найден/истёк.
        """
        ...

    @abstractmethod
    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        """Сохранить значение в кеш.

        Args:
            key: Уникальный ключ кеша.
            value: Значение для сохранения.
            ttl: Время жизни в секундах. None = бессрочно.
        """
        ...

    @abstractmethod
    def delete(self, key: str) -> bool:
        """Удалить значение по ключу.

        Args:
            key: Ключ для удаления.

        Returns:
            True если ключ существовал и был удалён.
        """
        ...

    @abstractmethod
    def exists(self, key: str) -> bool:
        """Проверить наличие ключа в кеше.

        Args:
            key: Ключ для проверки.

        Returns:
            True если ключ существует и не истёк.
        """
        ...

    @abstractmethod
    def clear(self) -> None:
        """Полностью очистить кеш."""
        ...


class NullCache(ICache):
    """Заглушка — ничего не кеширует.

    Используется когда модуль кеша не загружен.
    Все методы — no-op. get() всегда возвращает None.
    Гарантирует что код, использующий state.cache, работает
    без проверки "загружен ли кеш-модуль".
    """

    def get(self, key: str) -> Any | None:
        return None

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        pass

    def delete(self, key: str) -> bool:
        return False

    def exists(self, key: str) -> bool:
        return False

    def clear(self) -> None:
        pass
