"""Cache Interface — Port для реализации кеш-адаптеров.

Mia определяет ИНТЕРФЕЙС в core/interfaces.py (ICache).
Этот модуль содержит NullCache (заглушку) и может быть расширен
пользовательскими реализациями (Redis, Memory, File).

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

from typing import Any

# Единственный источник ICache — core/interfaces.py (Dependency Rule)
from core.interfaces import ICache  # noqa: F401 — re-export для обратной совместимости

__all__ = ["ICache", "NullCache"]


class NullCache(ICache):
    """Заглушка — ничего не кеширует.

    Используется когда модуль кеша не загружен.
    Все методы — no-op. get() всегда возвращает None.
    Гарантирует что код, использующий state.cache, работает
    без проверки «загружен ли кеш-модуль».
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
