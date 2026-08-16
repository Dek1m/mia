"""Юнит-тесты для MiaConfig (core/config.py).

Покрывает:
- Все 32 дефолта (сверка с _build_defaults)
- JSON5 парсер (комментарии, trailing commas)
- Загрузка из файла (merge с defaults)
- ENV overlay (MIA_* переопределяет)
- Каскад приоритетов: ENV > файл > defaults
- Определение пути (MIA_CONFIG_PATH, explicit path)
- Обработка ошибок (битый файл → defaults, невалидный ENV)
- Таблица _ENV_TO_DOTPATH
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from core.config import (
    MiaConfig,
    _parse_json5,
    _deep_merge,
    _set_by_dotpath,
    _cast_env_value,
    _ENV_TO_DOTPATH,
    _NUMERIC_KEYS,
)


# ── Фикстуры ───────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Сброс singleton перед каждым тестом."""
    MiaConfig.reset()
    yield
    MiaConfig.reset()


@pytest.fixture(autouse=True)
def _clean_env():
    """Очистка ENV от MIA_* переменных перед каждым тестом."""
    saved = {k: v for k, v in os.environ.items() if k.startswith("MIA_")}
    for k in saved:
        os.environ.pop(k, None)
    yield
    for k, v in saved.items():
        os.environ[k] = v


# ── Тесты дефолтов ─────────────────────────────────────────────────


class TestDefaults:
    """Проверка что все 32 дефолта совпадают с текущими значениями."""

    def test_all_defaults_present(self):
        """Все 32 уникальных dotpath присутствуют в _build_defaults."""
        cfg = MiaConfig.load()
        expected_dotpaths = [
            "core.routing.p95_threshold",
            "core.routing.history_window",
            "core.task_store.max_size",
            "core.task_store.history_limit",
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
            "pools.thread_pool.max_workers",  # default = None
            "resilience.retry.max_attempts",
            "resilience.retry.base_delay",
            "resilience.retry.max_delay",
            "resilience.circuit_breaker.failure_threshold",
            "resilience.circuit_breaker.recovery_timeout",
            "resilience.circuit_breaker.success_threshold",
            "monitoring.heartbeat.timeout",
            "monitoring.heartbeat.check_interval",
            "modules.dir",
            "storage.cache.backend",
            "modules.verification.mode",
        ]
        for dotpath in expected_dotpaths:
            # max_workers = None — это валидный дефолт (=cpu_count)
            if dotpath == "pools.thread_pool.max_workers":
                assert cfg.get_value(dotpath) is None, (
                    f"Default for '{dotpath}' should be None"
                )
            else:
                value = cfg.get_value(dotpath)
                assert value is not None, f"Default for '{dotpath}' is None"

    def test_numeric_defaults_exact_values(self):
        """Точные значения числовых дефолтов (сверка с хардкодом)."""
        cfg = MiaConfig.load()
        expected = {
            "core.routing.p95_threshold": 0.1,
            "core.routing.history_window": 1000,
            "core.task_store.max_size": 25000,
            "core.task_store.history_limit": 100,
            "core.stats_writer.batch_size": 500,
            "core.stats_writer.flush_interval": 5.0,
            "core.stats_writer.stop_timeout": 10.0,
            "core.task.timeout": 10.0,
            "core.task.retry": 0,
            "core.task.retry_delay": 0.5,
            "core.shutdown.timeout": 30.0,
            "pools.worker.stop_timeout": 5.0,
            "core.database.list_limit": 100,
            "modules.max_init_size": 1000000,
            "pools.load_balancer.weight_cpu": 0.7,
            "pools.load_balancer.weight_tasks": 0.2,
            "pools.load_balancer.weight_stale": 0.1,
            "pools.load_balancer.max_active_tasks": 10,
            "pools.cpu_metrics.collect_interval": 1.0,
            "pools.worker.heartbeat_period": 5.0,
            "pools.thread_pool.max_workers": None,
            "resilience.retry.max_attempts": 3,
            "resilience.retry.base_delay": 0.5,
            "resilience.retry.max_delay": 30.0,
            "resilience.circuit_breaker.failure_threshold": 5,
            "resilience.circuit_breaker.recovery_timeout": 30.0,
            "resilience.circuit_breaker.success_threshold": 3,
            "monitoring.heartbeat.timeout": 30.0,
            "monitoring.heartbeat.check_interval": 5.0,
            "modules.dir": "modules",
            "storage.cache.backend": "null",
            "modules.verification.mode": "disabled",
        }
        for dotpath, expected_value in expected.items():
            actual = cfg.get_value(dotpath)
            assert actual == expected_value, (
                f"Default for '{dotpath}': expected {expected_value!r}, got {actual!r}"
            )


# ── Тесты JSON5 парсера ────────────────────────────────────────────


class TestJson5Parsing:
    """Парсинг JSON5: комментарии, trailing commas."""

    def test_single_line_comments(self):
        """Однострочные комментарии // удаляются."""
        text = '{"a": 1, // comment\n"b": 2}'
        result = _parse_json5(text)
        assert result == {"a": 1, "b": 2}

    def test_multi_line_comments(self):
        """Многострочные комментарии /* */ удаляются."""
        text = '{"a": 1, /* multi\nline\ncomment */ "b": 2}'
        result = _parse_json5(text)
        assert result == {"a": 1, "b": 2}

    def test_trailing_comma_object(self):
        """Trailing comma в объекте: {a: 1,} → {a: 1}."""
        text = '{"a": 1, "b": 2,}'
        result = _parse_json5(text)
        assert result == {"a": 1, "b": 2}

    def test_trailing_comma_array(self):
        """Trailing comma в массиве: [1, 2,] → [1, 2]."""
        text = '[1, 2,]'
        result = _parse_json5(text)
        assert result == [1, 2]

    def test_trailing_comma_nested(self):
        """Trailing comma во вложенных структурах."""
        text = '{"a": {"b": [1, 2,],},}'
        result = _parse_json5(text)
        assert result == {"a": {"b": [1, 2]}}

    def test_combined_comments_and_trailing_commas(self):
        """Комментарии + trailing commas вместе."""
        text = """{
            // Core settings
            "core": {
                "timeout": 30.0, // timeout value
                /* list of items */
                "items": [1, 2, 3,],
            }, // end core
        }"""
        result = _parse_json5(text)
        assert result == {"core": {"timeout": 30.0, "items": [1, 2, 3]}}

    def test_valid_json_passthrough(self):
        """Валидный JSON парсится без изменений."""
        text = '{"a": 1, "b": "hello", "c": true, "d": null}'
        result = _parse_json5(text)
        assert result == {"a": 1, "b": "hello", "c": True, "d": None}

    def test_comment_inside_string_not_removed(self):
        """// внутри строк (URL) не удаляется парсером.

        После фикса: строки извлекаются в плейсхолдеры ДО удаления комментариев,
        поэтому // внутри строк не затрагивается.
        """
        text = '{"url": "http://example.com"}'
        result = _parse_json5(text)
        assert result == {"url": "http://example.com"}

    def test_comment_inside_string_actually_breaks(self):
        """// в строке больше не ломает парсинг (строки защищены плейсхолдерами)."""
        text = '{"url": "http://example.com"}'
        result = _parse_json5(text)
        assert result == {"url": "http://example.com"}


# ── Тесты загрузки из файла ────────────────────────────────────────


class TestFileLoading:
    """Загрузка конфига из JSON5 файла."""

    def test_file_loading_merges_with_defaults(self, tmp_path: Path):
        """Файл merge с defaults: файл переопределяет только указанные ключи."""
        config_file = tmp_path / "mia.json5"
        config_file.write_text('{"core": {"task": {"timeout": 99.0}}}')

        cfg = MiaConfig.load(config_path=config_file)

        # Изменённое значение из файла
        assert cfg.get_value("core.task.timeout") == 99.0
        # Остальные дефолты на месте
        assert cfg.get_value("core.task.retry") == 0
        assert cfg.get_value("core.task.retry_delay") == 0.5

    def test_file_loading_preserves_all_defaults(self, tmp_path: Path):
        """Файл с одним ключом не ломает остальные дефолты."""
        config_file = tmp_path / "mia.json5"
        config_file.write_text('{"core": {"routing": {"p95_threshold": 0.5}}}')

        cfg = MiaConfig.load(config_path=config_file)

        assert cfg.get_value("core.routing.p95_threshold") == 0.5
        assert cfg.get_value("core.routing.history_window") == 1000
        assert cfg.get_value("pools.load_balancer.weight_cpu") == 0.7

    def test_file_loading_empty_object(self, tmp_path: Path):
        """Пустой файл {} — все дефолты на месте."""
        config_file = tmp_path / "mia.json5"
        config_file.write_text('{}')

        cfg = MiaConfig.load(config_path=config_file)

        assert cfg.get_value("core.task.timeout") == 10.0
        assert cfg.get_value("modules.dir") == "modules"

    def test_file_with_json5_syntax(self, tmp_path: Path):
        """Файл с комментариями и trailing commas парсится."""
        config_file = tmp_path / "mia.json5"
        config_file.write_text("""{
            // Mia config
            "core": {
                "task": {
                    "timeout": 20.0, // double timeout
                },
            },
        }""")

        cfg = MiaConfig.load(config_path=config_file)

        assert cfg.get_value("core.task.timeout") == 20.0


# ── Тесты ENV overlay ──────────────────────────────────────────────


class TestEnvOverlay:
    """ENV MIA_* переопределяет defaults и файл."""

    def test_env_overrides_default(self, monkeypatch):
        """ENV переменная переопределяет дефолт."""
        monkeypatch.setenv("MIA_TASK_TIMEOUT", "25.0")

        cfg = MiaConfig.load()

        assert cfg.get_value("core.task.timeout") == 25.0

    def test_env_priority_over_file(self, tmp_path: Path, monkeypatch):
        """ENV > файл: ENV переопределяет значение из файла."""
        config_file = tmp_path / "mia.json5"
        config_file.write_text('{"core": {"task": {"timeout": 99.0}}}')
        monkeypatch.setenv("MIA_TASK_TIMEOUT", "42.0")

        cfg = MiaConfig.load(config_path=config_file)

        assert cfg.get_value("core.task.timeout") == 42.0

    def test_env_numeric_int(self, monkeypatch):
        """ENV числовое значение конвертируется в int."""
        monkeypatch.setenv("MIA_LB_MAX_ACTIVE_TASKS", "20")

        cfg = MiaConfig.load()

        assert cfg.get_value("pools.load_balancer.max_active_tasks") == 20
        assert isinstance(cfg.get_value("pools.load_balancer.max_active_tasks"), int)

    def test_env_numeric_float(self, monkeypatch):
        """ENV числовое значение с точкой конвертируется в float."""
        monkeypatch.setenv("MIA_LB_WEIGHT_CPU", "0.9")

        cfg = MiaConfig.load()

        assert cfg.get_value("pools.load_balancer.weight_cpu") == 0.9
        assert isinstance(cfg.get_value("pools.load_balancer.weight_cpu"), float)

    def test_env_string_value(self, monkeypatch):
        """ENV строковое значение остаётся строкой."""
        monkeypatch.setenv("MIA_CACHE_BACKEND", "hierarchy")

        cfg = MiaConfig.load()

        assert cfg.get_value("storage.cache.backend") == "hierarchy"

    def test_env_null_for_thread_pool(self, monkeypatch):
        """ENV MIA_THREAD_POOL_MAX_WORKERS=null → None."""
        monkeypatch.setenv("MIA_THREAD_POOL_MAX_WORKERS", "null")

        cfg = MiaConfig.load()

        assert cfg.get_value("pools.thread_pool.max_workers") is None

    def test_env_none_for_thread_pool(self, monkeypatch):
        """ENV MIA_THREAD_POOL_MAX_WORKERS=none → None."""
        monkeypatch.setenv("MIA_THREAD_POOL_MAX_WORKERS", "none")

        cfg = MiaConfig.load()

        assert cfg.get_value("pools.thread_pool.max_workers") is None


# ── Тесты каскада ──────────────────────────────────────────────────


class TestCascade:
    """Полный каскад: defaults → файл → ENV."""

    def test_full_cascade(self, tmp_path: Path, monkeypatch):
        """Полный каскад: defaults + файл + ENV."""
        config_file = tmp_path / "mia.json5"
        # Файл: timeout=99, retry=5
        config_file.write_text('{"core": {"task": {"timeout": 99.0, "retry": 5}}}')
        # ENV: timeout=42
        monkeypatch.setenv("MIA_TASK_TIMEOUT", "42.0")

        cfg = MiaConfig.load(config_path=config_file)

        # ENV побеждает файл
        assert cfg.get_value("core.task.timeout") == 42.0
        # Файл побеждает defaults
        assert cfg.get_value("core.task.retry") == 5
        # Defaults на месте
        assert cfg.get_value("core.task.retry_delay") == 0.5

    def test_no_file_no_env_uses_defaults(self):
        """Без файла и ENV — используются defaults."""
        cfg = MiaConfig.load()

        assert cfg.get_value("core.task.timeout") == 10.0
        assert cfg.get_value("pools.load_balancer.weight_cpu") == 0.7
        assert cfg.get_value("monitoring.heartbeat.timeout") == 30.0


# ── Тесты определения пути ────────────────────────────────────────


class TestConfigPath:
    """Определение пути к конфигу."""

    def test_explicit_path_priority(self, tmp_path: Path, monkeypatch):
        """Явный config_path приоритетнее MIA_CONFIG_PATH."""
        explicit = tmp_path / "explicit.json5"
        explicit.write_text('{"core": {"task": {"timeout": 111.0}}}')
        env_path = tmp_path / "env.json5"
        env_path.write_text('{"core": {"task": {"timeout": 222.0}}}')
        monkeypatch.setenv("MIA_CONFIG_PATH", str(env_path))

        cfg = MiaConfig.load(config_path=explicit)

        assert cfg.get_value("core.task.timeout") == 111.0

    def test_env_path_used_when_no_explicit(self, tmp_path: Path, monkeypatch):
        """MIA_CONFIG_PATH используется если нет явного config_path."""
        env_path = tmp_path / "env_config.json5"
        env_path.write_text('{"core": {"task": {"timeout": 333.0}}}')
        monkeypatch.setenv("MIA_CONFIG_PATH", str(env_path))

        cfg = MiaConfig.load()

        assert cfg.get_value("core.task.timeout") == 333.0

    def test_no_file_no_env_path(self):
        """Без config_path и MIA_CONFIG_PATH — defaults."""
        cfg = MiaConfig.load()

        assert cfg.get_value("core.task.timeout") == 10.0

    def test_nonexistent_explicit_path_uses_defaults(self, tmp_path: Path):
        """Несуществующий explicit path — defaults (не падает)."""
        fake = tmp_path / "nonexistent.json5"

        cfg = MiaConfig.load(config_path=fake)

        assert cfg.get_value("core.task.timeout") == 10.0


# ── Тесты обработки ошибок ────────────────────────────────────────


class TestErrorHandling:
    """Обработка ошибок: битый файл, невалидные ENV."""

    def test_invalid_json5_file_uses_defaults(self, tmp_path: Path, caplog):
        """Битый JSON5 файл → лог + defaults (не падает)."""
        config_file = tmp_path / "mia.json5"
        config_file.write_text("{ invalid json!!! {{{")

        cfg = MiaConfig.load(config_path=config_file)

        # Defaults на месте
        assert cfg.get_value("core.task.timeout") == 10.0
        # Есть лог о ошибке
        assert any("Failed to parse" in r.message or "parse" in r.message.lower()
                    for r in caplog.records)

    def test_unknown_env_numeric_returns_string(self, monkeypatch, caplog):
        """Невалидное числовое ENV значение → строка + warning (не падает)."""
        monkeypatch.setenv("MIA_LB_WEIGHT_CPU", "abc")

        cfg = MiaConfig.load()

        # Значение остаётся строкой (fallback)
        assert cfg.get_value("pools.load_balancer.weight_cpu") == "abc"
        # Есть warning
        assert any("Invalid numeric" in r.message or "invalid" in r.message.lower()
                    for r in caplog.records)

    def test_unknown_env_key_ignored(self, monkeypatch):
        """MIA_ переменная не из таблицы → игнорируется."""
        monkeypatch.setenv("MIA_UNKNOWN_SOMETHING", "value")

        cfg = MiaConfig.load()

        # Не создаёт никаких ключей
        assert cfg.get_value("unknown") is None

    def test_empty_env_value_for_non_string(self, monkeypatch):
        """Пустая строка для не-строкового ключа → строка ''."""
        monkeypatch.setenv("MIA_LB_WEIGHT_CPU", "")

        cfg = MiaConfig.load()

        # Пустая строка не конвертируется в float
        assert cfg.get_value("pools.load_balancer.weight_cpu") == ""


# ── Тесты get_value ────────────────────────────────────────────────


class TestGetValue:
    """Метод get_value(dotpath)."""

    def test_get_value_nested(self, tmp_path: Path):
        """Вложенные dotpath возвращают правильные значения."""
        config_file = tmp_path / "mia.json5"
        config_file.write_text('{"core": {"task": {"timeout": 42.0}}}')

        cfg = MiaConfig.load(config_path=config_file)

        assert cfg.get_value("core.task.timeout") == 42.0

    def test_get_value_default(self):
        """Несуществующий dotpath → default."""
        cfg = MiaConfig.load()

        assert cfg.get_value("nonexistent.key", "fallback") == "fallback"

    def test_get_value_none_default(self):
        """Несуществующий dotpath без default → None."""
        cfg = MiaConfig.load()

        assert cfg.get_value("nonexistent.key") is None

    def test_get_value_shallow(self):
        """Короткий dotpath (1 уровень)."""
        cfg = MiaConfig.load()

        assert cfg.get_value("modules") == {"dir": "modules", "max_init_size": 1000000, "verification": {"mode": "disabled"}}


# ── Тесты _ENV_TO_DOTPATH ──────────────────────────────────────────


class TestEnvMapping:
    """Таблица _ENV_TO_DOTPATH."""

    def test_env_mapping_count(self):
        """Таблица содержит 32 записи."""
        assert len(_ENV_TO_DOTPATH) == 32

    def test_env_mapping_compound_names(self):
        """Составные имена маппятся корректно (max_active_tasks, check_interval)."""
        assert _ENV_TO_DOTPATH["MIA_LB_MAX_ACTIVE_TASKS"] == "pools.load_balancer.max_active_tasks"
        assert _ENV_TO_DOTPATH["MIA_HEARTBEAT_CHECK_INTERVAL"] == "monitoring.heartbeat.check_interval"
        assert _ENV_TO_DOTPATH["MIA_STATS_WRITER_FLUSH_INTERVAL"] == "core.stats_writer.flush_interval"

    def test_numeric_keys_count(self):
        """_NUMERIC_KEYS содержит все числовые dotpath."""
        # Все dotpath из _ENV_TO_DOTPATH кроме строковых
        string_keys = {"modules.dir", "storage.cache.backend", "modules.verification.mode"}
        expected_numeric = set(_ENV_TO_DOTPATH.values()) - string_keys
        assert _NUMERIC_KEYS == expected_numeric

    def test_all_env_vars_have_dotpath(self):
        """Каждая ENV-переменная из таблицы приводит к значению."""
        os.environ["MIA_ROUTING_P95_THRESHOLD"] = "0.5"
        try:
            cfg = MiaConfig.load()
            assert cfg.get_value("core.routing.p95_threshold") == 0.5
        finally:
            del os.environ["MIA_ROUTING_P95_THRESHOLD"]

    def test_dotpath_set_by_dotpath(self):
        """_set_by_dotpath создаёт вложенные dict."""
        data: dict = {}
        _set_by_dotpath(data, "a.b.c", 42)
        assert data == {"a": {"b": {"c": 42}}}

    def test_dotpath_set_overwrites(self):
        """_set_by_dotpath перезаписывает существующее значение."""
        data: dict = {"a": {"b": 1}}
        _set_by_dotpath(data, "a.b", 2)
        assert data == {"a": {"b": 2}}

    def test_cast_env_value_float(self):
        """_cast_env_value конвертирует float строку."""
        assert _cast_env_value("3.14", "core.task.timeout") == 3.14

    def test_cast_env_value_int(self):
        """_cast_env_value конвертирует int строку."""
        assert _cast_env_value("42", "core.task.retry") == 42

    def test_cast_env_value_string(self):
        """_cast_env_value оставляет строку для не-числовых ключей."""
        assert _cast_env_value("hierarchy", "storage.cache.backend") == "hierarchy"

    def test_cast_env_value_null_thread_pool(self):
        """_cast_env_value для thread_pool.max_workers = null → None."""
        assert _cast_env_value("null", "pools.thread_pool.max_workers") is None


# ── Тесты deep_merge ───────────────────────────────────────────────


class TestDeepMerge:
    """Рекурсивный merge."""

    def test_deep_merge_basic(self):
        """Базовый merge: override переопределяет base."""
        base = {"a": 1, "b": 2}
        override = {"b": 3, "c": 4}
        result = _deep_merge(base, override)
        assert result == {"a": 1, "b": 3, "c": 4}

    def test_deep_merge_nested(self):
        """Вложенный merge."""
        base = {"a": {"x": 1, "y": 2}}
        override = {"a": {"y": 3}}
        result = _deep_merge(base, override)
        assert result == {"a": {"x": 1, "y": 3}}

    def test_deep_merge_not_mutating(self):
        """_deep_merge не мутирует base."""
        base = {"a": 1}
        override = {"a": 2}
        _deep_merge(base, override)
        assert base == {"a": 1}

    def test_deep_merge_override_replaces_non_dict(self):
        """Override dict → non-dict заменяет значение."""
        base = {"a": {"x": 1}}
        override = {"a": "string"}
        result = _deep_merge(base, override)
        assert result == {"a": "string"}


# ── Тесты singleton ────────────────────────────────────────────────


class TestSingleton:
    """Singleton поведение."""

    def test_get_returns_same_instance(self):
        """MiaConfig.get() возвращает тот же экземпляр."""
        cfg1 = MiaConfig.get()
        cfg2 = MiaConfig.get()
        assert cfg1 is cfg2

    def test_reset_clears_singleton(self):
        """reset() сбрасывает singleton."""
        cfg1 = MiaConfig.get()
        MiaConfig.reset()
        cfg2 = MiaConfig.get()
        assert cfg1 is not cfg2

    def test_load_returns_fresh_instance(self):
        """load() создаёт новый экземпляр."""
        cfg1 = MiaConfig.load()
        cfg2 = MiaConfig.load()
        assert cfg1 is not cfg2
