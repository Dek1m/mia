"""Интеграционные тесты конфиг-системы Mia Framework.

Проверяет:
- Обратная совместимость: Application(), фабрики, дефолты
- Фабрики читают из MiaConfig
- Приоритет verification_mode
- LoadBalancer применяет веса из конфига
- modules.dir из конфига влияет на Application
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.config import MiaConfig
from core.application import Application
from core.factories import (
    HeartbeatFactory,
    CpuMetricsCollectorFactory,
    CacheFactory,
)
from modules_system.verification import VerificationMode


# ── Фикстуры ───────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_config():
    """Сброс singleton и ENV перед каждым тестом."""
    MiaConfig.reset()
    saved = {k: v for k, v in os.environ.items() if k.startswith("MIA_")}
    for k in saved:
        os.environ.pop(k, None)
    yield
    MiaConfig.reset()
    for k, v in saved.items():
        os.environ[k] = v


# ── Тесты обратной совместимости ───────────────────────────────────


class TestBackwardCompat:
    """Существующие API работают без изменений."""

    def test_application_creation_default(self):
        """Application() без параметров создаётся (defaults)."""
        app = Application()
        assert app is not None
        assert app.verification_mode == VerificationMode.DISABLED

    def test_application_modules_dir_default(self):
        """Application() использует modules_dir='modules' по умолчанию."""
        app = Application()
        assert app._modules_dir == "modules"

    def test_application_explicit_params(self):
        """Application(modules_dir='x', cache_backend='null') работает."""
        app = Application(modules_dir="modules", cache_backend="null")
        assert app._modules_dir == "modules"
        assert app._cache_backend == "null"

    def test_cache_factory_null_default(self):
        """CacheFactory.create() создаёт NullCache по умолчанию."""
        cache = CacheFactory.create()
        assert type(cache).__name__ == "NullCache"

    def test_cache_factory_explicit(self):
        """CacheFactory.create('null') работает."""
        cache = CacheFactory.create("null")
        assert type(cache).__name__ == "NullCache"


# ── Тесты фабрик ──────────────────────────────────────────────────


class TestFactoryIntegration:
    """Фабрики берут дефолты из MiaConfig."""

    def test_heartbeat_factory_default(self):
        """HeartbeatFactory: timeout=30.0, check_interval=5.0 из config."""
        monitor = HeartbeatFactory.create()
        # HeartbeatMonitor хранит timeout и check_interval
        assert monitor._timeout == 30.0
        assert monitor._check_interval == 5.0

    def test_heartbeat_factory_from_config(self, monkeypatch):
        """HeartbeatFactory: ENV MIA_HEARTBEAT_TIMEOUT=7 → timeout=7.0."""
        monkeypatch.setenv("MIA_HEARTBEAT_TIMEOUT", "7")
        MiaConfig.reset()
        monitor = HeartbeatFactory.create()
        assert monitor._timeout == 7.0

    def test_heartbeat_factory_explicit_overrides_config(self, monkeypatch):
        """HeartbeatFactory: explicit param > config."""
        monkeypatch.setenv("MIA_HEARTBEAT_TIMEOUT", "7")
        MiaConfig.reset()
        monitor = HeartbeatFactory.create(timeout=99.0)
        assert monitor._timeout == 99.0

    def test_cpu_metrics_factory_default(self):
        """CpuMetricsCollectorFactory: collect_interval=1.0 из config."""
        collector = CpuMetricsCollectorFactory.create()
        assert collector._collect_interval == 1.0

    def test_cpu_metrics_factory_from_config(self, monkeypatch):
        """CpuMetricsCollectorFactory: ENV MIA_CPU_COLLECT_INTERVAL=3.0."""
        monkeypatch.setenv("MIA_CPU_COLLECT_INTERVAL", "3.0")
        MiaConfig.reset()
        collector = CpuMetricsCollectorFactory.create()
        assert collector._collect_interval == 3.0


# ── Тесты verification_mode priority ───────────────────────────────


class TestVerificationPriority:
    """Приоритет verification_mode: параметр > ENV > файл > disabled."""

    def test_default_is_disabled(self):
        """По умолчанию verification_mode = DISABLED."""
        app = Application()
        assert app.verification_mode == VerificationMode.DISABLED

    def test_explicit_param_wins(self, monkeypatch):
        """Явный параметр > ENV."""
        monkeypatch.setenv("MIA_MODULE_VERIFICATION", "warn")
        MiaConfig.reset()
        app = Application(verification_mode=VerificationMode.STRICT)
        assert app.verification_mode == VerificationMode.STRICT

    def test_env_wins_over_config_file(self, tmp_path: Path, monkeypatch):
        """ENV > файл."""
        config_file = tmp_path / "mia.json5"
        config_file.write_text('{"modules": {"verification": {"mode": "warn"}}}')
        monkeypatch.setenv("MIA_MODULE_VERIFICATION", "strict")
        MiaConfig.reset()
        # Загружаем конфиг из файла
        MiaConfig.load(config_path=config_file)
        app = Application()
        assert app.verification_mode == VerificationMode.STRICT

    def test_config_file_wins_over_default(self, tmp_path: Path):
        """Файл > дефолт DISABLED."""
        config_file = tmp_path / "mia.json5"
        config_file.write_text('{"modules": {"verification": {"mode": "warn"}}}')
        MiaConfig.load(config_path=config_file)
        app = Application()
        assert app.verification_mode == VerificationMode.WARN

    def test_full_cascade(self, tmp_path: Path, monkeypatch):
        """Полный каскад: параметр > ENV > файл > disabled."""
        config_file = tmp_path / "mia.json5"
        config_file.write_text('{"modules": {"verification": {"mode": "disabled"}}}')
        monkeypatch.setenv("MIA_MODULE_VERIFICATION", "warn")
        MiaConfig.load(config_path=config_file)

        # Без параметра: ENV побеждает файл
        app1 = Application()
        assert app1.verification_mode == VerificationMode.WARN

        # С параметром: параметр побеждает ENV
        app2 = Application(verification_mode=VerificationMode.STRICT)
        assert app2.verification_mode == VerificationMode.STRICT


# ── Тесты LoadBalancer конфигурации ────────────────────────────────


class TestLoadBalancerConfig:
    """Веса LoadBalancer из конфига."""

    def test_load_balancer_default_weights(self):
        """LoadBalancer: дефолтные веса weight_cpu=0.7, weight_tasks=0.2, weight_stale=0.1."""
        from pools.load_balancer import LoadBalancer
        lb = LoadBalancer()
        assert lb.WEIGHT_CPU == 0.7
        assert lb.WEIGHT_TASKS == 0.2
        assert lb.WEIGHT_STALE == 0.1
        assert lb.MAX_ACTIVE_TASKS == 10

    def test_load_balancer_from_config(self, monkeypatch):
        """LoadBalancer: ENV MIA_LB_WEIGHT_CPU=0.9 → WEIGHT_CPU=0.9."""
        from pools.load_balancer import LoadBalancer
        monkeypatch.setenv("MIA_LB_WEIGHT_CPU", "0.9")
        monkeypatch.setenv("MIA_LB_MAX_ACTIVE_TASKS", "20")
        MiaConfig.reset()
        lb = LoadBalancer()
        assert lb.WEIGHT_CPU == 0.9
        assert lb.MAX_ACTIVE_TASKS == 20


# ── Тесты modules.dir ─────────────────────────────────────────────


class TestModulesDirConfig:
    """modules.dir из конфига влияет на Application."""

    def test_modules_dir_from_env(self, monkeypatch):
        """MIA_MODULES_DIR влияет на Application._modules_dir."""
        monkeypatch.setenv("MIA_MODULES_DIR", "my_modules")
        MiaConfig.reset()
        app = Application()
        assert app._modules_dir == "my_modules"

    def test_modules_dir_from_config_file(self, tmp_path: Path):
        """modules.dir из файла влияет на Application._modules_dir."""
        config_file = tmp_path / "mia.json5"
        config_file.write_text('{"modules": {"dir": "custom_modules"}}')
        MiaConfig.load(config_path=config_file)
        app = Application()
        assert app._modules_dir == "custom_modules"

    def test_explicit_modules_dir_wins(self, tmp_path: Path, monkeypatch):
        """Явный параметр > ENV > файл."""
        config_file = tmp_path / "mia.json5"
        config_file.write_text('{"modules": {"dir": "file_modules"}}')
        monkeypatch.setenv("MIA_MODULES_DIR", "env_modules")
        MiaConfig.load(config_path=config_file)
        app = Application(modules_dir="explicit_modules")
        assert app._modules_dir == "explicit_modules"

    def test_cache_backend_from_env(self, monkeypatch):
        """MIA_CACHE_BACKEND влияет на Application._cache_backend."""
        monkeypatch.setenv("MIA_CACHE_BACKEND", "null")
        MiaConfig.reset()
        app = Application()
        assert app._cache_backend == "null"


# ── Тесты с tmp_path модулями ──────────────────────────────────────


class TestApplicationWithCustomModulesDir:
    """Application с кастомной директорией модулей."""

    def test_application_with_custom_dir(self, tmp_path: Path):
        """Application(modules_dir=...) загружает модули из указанной директории."""
        # Создаём минимальный модуль
        modules_dir = tmp_path / "custom_modules"
        modules_dir.mkdir()
        mod_dir = modules_dir / "testmod"
        mod_dir.mkdir()
        (mod_dir / "__init__.py").write_text(
            'from modules_system.module_base import ModuleBase\n'
            'class M(ModuleBase):\n'
            '    @property\n'
            '    def name(self): return "testmod"\n',
            encoding="utf-8",
        )

        # DISABLED чтобы не нужен hash.json
        app = Application(modules_dir=str(modules_dir), verification_mode=VerificationMode.DISABLED)
        app.load_module("testmod")

        assert "testmod" in app.modules.list_all()
