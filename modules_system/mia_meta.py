"""MiaModuleMeta / BelleModuleMeta — декларативные метаданные модулей.

MiaModuleMeta:
    Базовый метакласс с дефолтами фреймворка. Содержит permissions,
    cache_rules, lock_rules, timeout_defaults, dependencies.

BelleModuleMeta:
    Наследует MiaModuleMeta, переопределяет дефолты из BelleConfig.

Использование::

    # В модуле
    from modules_system.mia_meta import BelleModuleMeta

    class MyModule(ModuleBase):
        @property
        def meta(self) -> BelleModuleMeta:
            return BelleModuleMeta(
                dependencies=["auth"],
                permissions={"get_user": "user.read"},
            )
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.belle_config import BelleConfig


@dataclass(frozen=True)
class MiaModuleMeta:
    """Базовый метакласс модуля Mia.

    Содержит дефолты фреймворка для всех модулей.
    Каждый модуль переопределяет только то, что отличается.
    """

    # Какие модули должны быть загружены до этого
    dependencies: list[str] = field(default_factory=list)

    # Требуемые permissions для методов: {"method_name": "permission.name"}
    permissions: dict[str, str] = field(default_factory=dict)

    # TTL кеширования результатов методов: {"method_name": seconds}
    cache_rules: dict[str, int] = field(default_factory=dict)

    # Шаблоны блокировок: {"method_name": "lock_template"}
    lock_rules: dict[str, str] = field(default_factory=dict)

    # Таймауты по умолчанию: {"method_name": seconds}
    timeout_defaults: dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_config(cls, config: BelleConfig) -> MiaModuleMeta:
        """Создать базовый мета-объект из конфигурации.

        Использует общие настройки core.task и pools.worker.
        """
        return cls(
            timeout_defaults={
                "default": config.core.task_timeout,
            },
        )


@dataclass(frozen=True)
class BelleModuleMeta(MiaModuleMeta):
    """Расширенный метакласс для модулей Belle.

    Наследует все поля MiaModuleMeta и добавляет belle-специфичные:
    - service_version: версия модуля из belle.toml
    - author: автор модуля
    - description: описание модуля

    Пример::

        @dataclass(frozen=True)
        class AuthMeta(BelleModuleMeta):
            jwt_algorithm: str = "HS256"
            access_ttl: int = 900

        class AuthModule(ModuleBase):
            @property
            def meta(self) -> AuthMeta:
                return AuthMeta(
                    dependencies=["db"],
                    permissions={"login": "auth.login"},
                    jwt_algorithm="ES256",
                )
    """

    # Мета-информация о модуле
    service_version: str = "0.0.0"
    author: str = ""
    description: str = ""

    @classmethod
    def from_config(cls, config: BelleConfig) -> BelleModuleMeta:
        """Создать мета-объект из BelleConfig.

        Использует auth-секцию для JWT-дефолтов, core.task для таймаутов.
        """
        return cls(
            timeout_defaults={
                "default": config.core.task_timeout,
                "login": config.auth.login_timeout,
                "create_user": config.auth.create_user_timeout,
            },
            cache_rules={
                "get_user": config.auth.get_user_ttl,
                "check_permission": config.auth.check_permission_ttl,
            },
        )
