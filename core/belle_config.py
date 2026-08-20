"""BelleConfig — единый конфигурационный источник для Belle.

Каскад загрузки:
1. Hardcoded defaults (секции dataclass'ов)
2. belle.toml (файл)
3. ENV BELLE_* / AUTH_* / DB_* (переменные окружения)

Приоритет: ENV > файл > defaults.

Пример использования::

    cfg = BelleConfig.load()
    # или
    cfg = BelleConfig.load(Path("/path/to/belle.toml"))
    secret = cfg.auth.jwt_secret
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# tomllib встроен в Python 3.11+, для 3.10 — внешний пакет tomli
if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        raise ImportError("Install 'tomli' for Python <3.11: pip install tomli")


@dataclass(frozen=True)
class MiaSection:
    """Секция [app] — базовые настройки модуля."""

    service_name: str = "belle"
    modules_dir: str = "modules"
    verification_mode: str = "disabled"


@dataclass(frozen=True)
class AuthSection:
    """Секция [auth] + [auth.cache] + [auth.timeouts]."""

    # JWT
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    access_token_ttl: int = 900
    refresh_token_ttl: int = 2592000

    # Пароли
    password_min_length: int = 8
    password_require_uppercase: bool = True
    password_require_digit: bool = True
    password_history_size: int = 10

    # Лимиты входа
    login_attempts_limit: int = 5
    login_block_minutes: int = 15

    # Кеш прав
    perms_cache_ttl: int = 300

    # Кеш пользователей
    get_user_ttl: int = 300
    check_permission_ttl: int = 60

    # Таймауты операций
    login_timeout: float = 10.0
    create_user_timeout: float = 5.0


@dataclass(frozen=True)
class DbSection:
    """Секция [db]."""

    host: str = "localhost"
    port: int = 5432
    database: str = "belle"
    user: str = "svc_athene_ai"
    pool_min: int = 2
    pool_max: int = 10


@dataclass(frozen=True)
class LlmSection:
    """Секция [llm]."""

    default_provider: str = "openai"
    timeout: float = 120.0


@dataclass(frozen=True)
class ApiProxySection:
    """Секция [apiproxy]."""

    whitelist: list[str] = field(default_factory=lambda: ["auth", "workspace", "llm"])
    method_timeout: float = 30.0


@dataclass(frozen=True)
class WorkspaceSection:
    """Секция [workspace]."""

    default_page_size: int = 50
    max_page_size: int = 200


@dataclass(frozen=True)
class LogSection:
    """Секция [log]."""

    level: str = "INFO"
    format: str = "posix"


@dataclass(frozen=True)
class CoreSection:
    """Секция [core.task] + дефолты routing/stats_writer/shutdown."""

    task_timeout: float = 10.0
    task_retry: int = 0
    p95_threshold: float = 0.1
    history_window: int = 1000


@dataclass(frozen=True)
class BelleConfig:
    """Единый конфигурационный источник Belle.

    Состоит из секций- Value Objects. Каждая секция — frozen dataclass.
    Загружается через ``BelleConfig.load()``.
    """

    app: MiaSection = field(default_factory=MiaSection)
    auth: AuthSection = field(default_factory=AuthSection)
    db: DbSection = field(default_factory=DbSection)
    llm: LlmSection = field(default_factory=LlmSection)
    apiproxy: ApiProxySection = field(default_factory=ApiProxySection)
    workspace: WorkspaceSection = field(default_factory=WorkspaceSection)
    log: LogSection = field(default_factory=LogSection)
    core: CoreSection = field(default_factory=CoreSection)

    @classmethod
    def load(cls, path: str | Path | None = None) -> BelleConfig:
        """Загрузить конфиг из belle.toml + ENV overrides.

        Args:
            path: Явный путь к belle.toml. Если None — auto-detect.

        Returns:
            Загруженный экземпляр BelleConfig.
        """
        data: dict[str, Any] = {}
        resolved = cls._resolve_path(path)

        if resolved is not None and resolved.exists():
            with open(resolved, "rb") as f:
                data = tomllib.load(f)

        # ENV overrides имеют наивысший приоритет
        data = cls._apply_env_overrides(data)

        return cls(
            app=MiaSection(**data.get("app", {})),
            auth=AuthSection(**cls._flatten_section(
                data.get("auth", {}),
                renames={"login": "login_timeout", "create_user": "create_user_timeout"},
            )),
            db=DbSection(**data.get("db", {})),
            llm=LlmSection(**data.get("llm", {})),
            apiproxy=ApiProxySection(**data.get("apiproxy", {})),
            workspace=WorkspaceSection(**data.get("workspace", {})),
            log=LogSection(**data.get("log", {})),
            core=CoreSection(**cls._flatten_section(
                data.get("core", {}),
                renames={"timeout": "task_timeout", "retry": "task_retry"},
            )),
        )

    @staticmethod
    def _resolve_path(explicit: str | Path | None) -> Path | None:
        """Определить путь к belle.toml.

        Приоритет:
        1. Явный параметр path
        2. ENV BELLE_CONFIG_PATH
        3. ./belle.toml (текущая директория)
        4. ../../belle.toml (корень проекта относительно core/)
        """
        if explicit:
            return Path(explicit)

        env_path = os.getenv("BELLE_CONFIG_PATH")
        if env_path:
            return Path(env_path)

        # Текущая директория
        local = Path("belle.toml")
        if local.exists():
            return local

        # Корень проекта (относительно расположения этого файла)
        project_root = Path(__file__).resolve().parent.parent / "belle.toml"
        if project_root.exists():
            return project_root

        return None

    @staticmethod
    def _flatten_section(
        data: dict[str, Any],
        renames: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Развести вложенные TOML-секции в плоский dict для dataclass.

        Args:
            data: Вложенный dict (TOML-секция).
            renames: Маппинг {nested_key: flat_key} для переименования
                ключей при развёртывании. Например,
                ``{"login": "login_timeout"}``.

        Пример::

            {"cache": {"get_user_ttl": 300}} → {"get_user_ttl": 300}
            {"timeouts": {"login": 10.0}}, renames={"login": "login_timeout"}
                → {"login_timeout": 10.0}
        """
        renames = renames or {}
        result: dict[str, Any] = {}
        for key, value in data.items():
            if isinstance(value, dict):
                for nested_key, nested_value in value.items():
                    flat_key = renames.get(nested_key, nested_key)
                    result[flat_key] = nested_value
            else:
                result[key] = value
        return result

    @staticmethod
    def _apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
        """Применить ENV overrides поверх данных из TOML.

        Маппинг ENV → (section, key):
        - BELLE_* → app.*
        - AUTH_* → auth.*
        - DB_* → db.*
        - MIA_LOG_* → log.*
        """
        env_map: dict[str, tuple[str, str]] = {
            "BELLE_SERVICE_NAME": ("app", "service_name"),
            "BELLE_MODULES_DIR": ("app", "modules_dir"),
            "BELLE_MODULE_VERIFICATION": ("app", "verification_mode"),
            # Auth
            "AUTH_JWT_SECRET": ("auth", "jwt_secret"),
            "AUTH_JWT_ALGORITHM": ("auth", "jwt_algorithm"),
            "AUTH_ACCESS_TOKEN_TTL": ("auth", "access_token_ttl"),
            "AUTH_REFRESH_TOKEN_TTL": ("auth", "refresh_token_ttl"),
            "AUTH_PASSWORD_MIN_LENGTH": ("auth", "password_min_length"),
            "AUTH_LOGIN_ATTEMPTS_LIMIT": ("auth", "login_attempts_limit"),
            "AUTH_LOGIN_BLOCK_MINUTES": ("auth", "login_block_minutes"),
            "AUTH_PERMS_CACHE_TTL": ("auth", "perms_cache_ttl"),
            # DB
            "DB_HOST": ("db", "host"),
            "DB_PORT": ("db", "port"),
            "DB_NAME": ("db", "database"),
            "DB_USER": ("db", "user"),
            "DB_PASSWORD": ("db", "password"),
            # LLM
            "BELLE_LLM_PROVIDER": ("llm", "default_provider"),
            "BELLE_LLM_TIMEOUT": ("llm", "timeout"),
            # Log
            "MIA_LOG_LEVEL": ("log", "level"),
            "BELLE_LOG_LEVEL": ("log", "level"),
        }

        # Типы для приведения строковых значений ENV
        int_keys = {
            "access_token_ttl", "refresh_token_ttl",
            "password_min_length", "login_attempts_limit",
            "login_block_minutes", "perms_cache_ttl",
            "port", "pool_min", "pool_max",
            "default_page_size", "max_page_size",
            "task_retry", "p95_threshold", "history_window",
        }
        float_keys = {
            "timeout", "login_timeout", "create_user_timeout",
            "method_timeout", "task_timeout",
        }

        for env_key, (section, key) in env_map.items():
            value = os.getenv(env_key)
            if value is None:
                continue

            if section not in data:
                data[section] = {}

            # Приведение типов
            if key in int_keys:
                try:
                    data[section][key] = int(value)
                except ValueError:
                    pass  # Оставляем строку, если не парсится
            elif key in float_keys:
                try:
                    data[section][key] = float(value)
                except ValueError:
                    pass
            else:
                data[section][key] = value

        return data
