"""MiaConfig — единый источник конфигурации Mia Framework.

Каскад загрузки:
1. Hardcoded defaults (_build_defaults)
2. mia.json5 (файл)
3. ENV MIA_* (переменные окружения)

Приоритет: ENV > файл > defaults.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Таблица соответствий ENV-имён → dotpath в конфиге.
# Используем явную таблицу вместо автоматического разбиения по _,
# чтобы не гадать с составными именами (например, max_active_tasks).
_ENV_TO_DOTPATH: dict[str, str] = {
    "MIA_ROUTING_P95_THRESHOLD": "core.routing.p95_threshold",
    "MIA_ROUTING_HISTORY_WINDOW": "core.routing.history_window",
    "MIA_STATS_WRITER_BATCH_SIZE": "core.stats_writer.batch_size",
    "MIA_STATS_WRITER_FLUSH_INTERVAL": "core.stats_writer.flush_interval",
    "MIA_STATS_WRITER_STOP_TIMEOUT": "core.stats_writer.stop_timeout",
    "MIA_TASK_TIMEOUT": "core.task.timeout",
    "MIA_TASK_RETRY": "core.task.retry",
    "MIA_TASK_RETRY_DELAY": "core.task.retry_delay",
    "MIA_SHUTDOWN_TIMEOUT": "core.shutdown.timeout",
    "MIA_WORKER_STOP_TIMEOUT": "pools.worker.stop_timeout",
    "MIA_DATABASE_LIST_LIMIT": "core.database.list_limit",
    "MIA_MODULE_MAX_INIT_SIZE": "modules.max_init_size",
    "MIA_ROUTING_STATS_UPDATE_INTERVAL": "core.routing.stats_update_interval",
    "MIA_LB_WEIGHT_CPU": "pools.load_balancer.weight_cpu",
    "MIA_LB_WEIGHT_TASKS": "pools.load_balancer.weight_tasks",
    "MIA_LB_WEIGHT_STALE": "pools.load_balancer.weight_stale",
    "MIA_LB_MAX_ACTIVE_TASKS": "pools.load_balancer.max_active_tasks",
    "MIA_CPU_COLLECT_INTERVAL": "pools.cpu_metrics.collect_interval",
    "MIA_WORKER_HEARTBEAT_PERIOD": "pools.worker.heartbeat_period",
    "MIA_RETRY_MAX_ATTEMPTS": "resilience.retry.max_attempts",
    "MIA_RETRY_BASE_DELAY": "resilience.retry.base_delay",
    "MIA_RETRY_MAX_DELAY": "resilience.retry.max_delay",
    "MIA_CB_FAILURE_THRESHOLD": "resilience.circuit_breaker.failure_threshold",
    "MIA_CB_RECOVERY_TIMEOUT": "resilience.circuit_breaker.recovery_timeout",
    "MIA_CB_SUCCESS_THRESHOLD": "resilience.circuit_breaker.success_threshold",
    "MIA_HEARTBEAT_TIMEOUT": "monitoring.heartbeat.timeout",
    "MIA_HEARTBEAT_CHECK_INTERVAL": "monitoring.heartbeat.check_interval",
    "MIA_MODULES_DIR": "modules.dir",
    "MIA_CACHE_BACKEND": "storage.cache.backend",
    "MIA_MODULE_VERIFICATION": "modules.verification.mode",
}

# Типы значений для приведения из строк ENV
_NUMERIC_KEYS: set[str] = {
    "core.routing.p95_threshold",
    "core.routing.history_window",
    "core.routing.stats_update_interval",
    "core.stats_writer.batch_size",
    "core.stats_writer.flush_interval",
    "core.stats_writer.stop_timeout",
    "core.task.timeout",
    "core.task.retry",
    "core.task.retry_delay",
    "core.shutdown.timeout",
    "pools.worker.stop_timeout",
    "core.database.list_limit",
    "modules.max_init_size",
    "pools.load_balancer.weight_cpu",
    "pools.load_balancer.weight_tasks",
    "pools.load_balancer.weight_stale",
    "pools.load_balancer.max_active_tasks",
    "pools.cpu_metrics.collect_interval",
    "pools.worker.heartbeat_period",
    "resilience.retry.max_attempts",
    "resilience.retry.base_delay",
    "resilience.retry.max_delay",
    "resilience.circuit_breaker.failure_threshold",
    "resilience.circuit_breaker.recovery_timeout",
    "resilience.circuit_breaker.success_threshold",
    "monitoring.heartbeat.timeout",
    "monitoring.heartbeat.check_interval",
}


class MiaConfig:
    """Единый источник конфигурации Mia.

    Singleton: используйте ``MiaConfig.get()`` для получения текущего
    экземпляра. Если экземпляр ещё не создан — загружается автоматически
    из каскада defaults → файл → ENV.

    Пример::

        cfg = MiaConfig.get()
        timeout = cfg.get_value("monitoring.heartbeat.timeout", 30.0)
    """

    _instance: MiaConfig | None = None

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    @classmethod
    def load(cls, config_path: str | Path | None = None) -> MiaConfig:
        """Загрузить конфиг с каскадом defaults → файл → ENV.

        Args:
            config_path: Путь к mia.json5. None → auto-detect.

        Returns:
            Загруженный экземпляр MiaConfig.
        """
        config = cls()
        config._data = config._build_defaults()

        # Шаг 2: загрузка из файла
        path = config._resolve_path(config_path)
        if path is not None and path.exists():
            try:
                file_data = _parse_json5(path.read_text(encoding="utf-8"))
                config._data = _deep_merge(config._data, file_data)
                log.info("Config loaded from file", extra={"path": str(path)})
            except Exception as e:
                log.warning(
                    "Failed to parse config file, using defaults",
                    extra={"path": str(path), "error": str(e)},
                )

        # Шаг 3: overlay из ENV
        env_data = config._load_env()
        if env_data:
            config._data = _deep_merge(config._data, env_data)
            log.debug("Config ENV overlay applied", extra={"keys": list(env_data.keys())})

        cls._instance = config
        return config

    @classmethod
    def get(cls) -> MiaConfig:
        """Получить текущий экземпляр (singleton).

        Если экземпляр ещё не создан — загружается автоматически.
        """
        if cls._instance is None:
            cls._instance = cls.load()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Сбросить singleton (для тестов)."""
        cls._instance = None

    def _build_defaults(self) -> dict[str, Any]:
        """Хардкод-дефолты (текущие значения из кода)."""
        return {
            "core": {
                "routing": {
                    "p95_threshold": 0.1,
                    "history_window": 1000,
                    "stats_update_interval": 100,
                },
                "stats_writer": {
                    "batch_size": 500,
                    "flush_interval": 5.0,
                    "stop_timeout": 10.0,
                },
                "task": {
                    "timeout": 10.0,
                    "retry": 0,
                    "retry_delay": 0.5,
                },
                "shutdown": {
                    "timeout": 30.0,
                },
                "database": {
                    "list_limit": 100,
                },
            },
            "pools": {
                "load_balancer": {
                    "weight_cpu": 0.7,
                    "weight_tasks": 0.2,
                    "weight_stale": 0.1,
                    "max_active_tasks": 10,
                },
                "cpu_metrics": {
                    "collect_interval": 1.0,
                },
                "worker": {
                    "heartbeat_period": 5.0,
                    "stop_timeout": 5.0,
                },
            },
            "resilience": {
                "retry": {
                    "max_attempts": 3,
                    "base_delay": 0.5,
                    "max_delay": 30.0,
                },
                "circuit_breaker": {
                    "failure_threshold": 5,
                    "recovery_timeout": 30.0,
                    "success_threshold": 3,
                },
            },
            "monitoring": {
                "heartbeat": {
                    "timeout": 30.0,
                    "check_interval": 5.0,
                },
            },
            "modules": {
                "dir": "modules",
                "max_init_size": 1000000,
                "verification": {
                    "mode": "disabled",
                },
            },
            "storage": {
                "cache": {
                    "backend": "null",
                },
            },
            "shared_memory": {
                "backend": "local",
                "redis_url": "redis://localhost:6379",
                "redis_prefix": "mia:",
                "result_ttl": 300,
            },
        }

    def _resolve_path(self, explicit: str | Path | None) -> Path | None:
        """Определить путь к конфигу.

        Приоритет:
        1. Явный параметр config_path
        2. ENV MIA_CONFIG_PATH
        3. ./mia.json5 (текущая директория)
        """
        if explicit:
            return Path(explicit)

        env_path = os.getenv("MIA_CONFIG_PATH")
        if env_path:
            return Path(env_path)

        local = Path("./mia.json5")
        if local.exists():
            return local

        return None

    def _load_env(self) -> dict[str, Any]:
        """Прочитать MIA_* переменные и преобразовать в nested dict.

        Использует явную таблицу _ENV_TO_DOTPATH для маппинга
        ENV-имён → dotpath, чтобы избежать проблем с составными именами.
        """
        result: dict[str, Any] = {}
        for env_name, dotpath in _ENV_TO_DOTPATH.items():
            value = os.getenv(env_name)
            if value is None:
                continue
            parsed = _cast_env_value(value, dotpath)
            _set_by_dotpath(result, dotpath, parsed)
        return result

    def get_value(self, dotpath: str, default: Any = None) -> Any:
        """Получить значение по dotted path.

        Args:
            dotpath: Путь вида "core.routing.p95_threshold".
            default: Значение по умолчанию, если путь не найден.

        Returns:
            Значение или default.
        """
        keys = dotpath.split(".")
        current: Any = self._data
        for key in keys:
            if isinstance(current, dict) and key in current:
                current = current[key]
            else:
                return default
        return current


# ── Вспомогательные функции ────────────────────────────────────────


def _parse_json5(text: str) -> dict[str, Any]:
    """Парсинг JSON5: удаление комментариев + trailing commas → JSON.

    Покрывает: /* */, //, trailing commas.
    Не нуждается во внешних зависимостях.

    Подход:
    1. Извлечь все строки ("..." и '...') в плейсхолдеры __MIA_STR_N__.
    2. Удалить комментарии (/* */ и //) — теперь они не внутри строк.
    3. Вернуть строки на место.
    4. Удалить trailing commas.
    5. json.loads.
    """
    # ── Шаг 1: извлечение строк в плейсхолдеры ──────────────────────
    # Используем уникальный префикс, который маловероятен в реальном тексте.
    _PLACEHOLDER_PREFIX = "__MIASTR"

    strings: list[str] = []  # оригинальные строки (с кавычками)

    def _replace_string(match: re.Match) -> str:
        """Заменить строку на плейсхолдер, сохранив оригинал."""
        idx = len(strings)
        strings.append(match.group(0))
        return f'{_PLACEHOLDER_PREFIX}{idx}__'

    # Обрабатываем двойные кавычки: "..." с учётом экранирования (\")
    # и одинарные кавычки: '...' с учётом экранирования (\')
    text = re.sub(
        r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'',
        _replace_string,
        text,
    )

    # ── Шаг 2: удаление комментариев ────────────────────────────────
    # Теперь строки извлечены, // и /* */ не могут быть внутри них.
    # 2a. Многострочные комментарии /* ... */
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    # 2b. Однострочные комментарии // ...
    text = re.sub(r"//.*?$", "", text, flags=re.MULTILINE)

    # ── Шаг 3: возврат строк на место ───────────────────────────────
    for idx, original in enumerate(strings):
        text = text.replace(f"{_PLACEHOLDER_PREFIX}{idx}__", original, 1)

    # ── Шаг 4: удаление trailing commas ─────────────────────────────
    # ,} → } и ,] → ]
    text = re.sub(r",\s*([}\]])", r"\1", text)

    # ── Шаг 5: парсинг валидного JSON ───────────────────────────────
    return json.loads(text)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Рекурсивный merge override в base (не мутирует base)."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _set_by_dotpath(data: dict[str, Any], dotpath: str, value: Any) -> None:
    """Установить значение по dotted path, создавая промежуточные dict."""
    keys = dotpath.split(".")
    current = data
    for key in keys[:-1]:
        if key not in current or not isinstance(current[key], dict):
            current[key] = {}
        current = current[key]
    current[keys[-1]] = value


def _cast_env_value(value: str, dotpath: str) -> Any:
    """Привести строковое значение ENV к нужному типу."""
    if dotpath in _NUMERIC_KEYS:
        try:
            if "." in value:
                return float(value)
            return int(value)
        except ValueError:
            log.warning(
                "Invalid numeric env value",
                extra={"dotpath": dotpath, "value": value},
            )
            return value
    return value
