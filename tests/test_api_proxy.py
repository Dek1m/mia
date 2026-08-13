"""Unit-тесты для ApiProxy."""
import pytest
from unittest.mock import patch, MagicMock

from communication.api_proxy import ApiProxy, ModuleApiProxy, ApiMethodProxy
from modules_system.module_base import ModuleBase, api_method
from modules.sample import SampleModule


class TestApiProxyCreation:
    """Тесты создания ApiProxy."""

    def test_api_proxy_creation(self):
        """ApiProxy() создаётся без ошибок."""
        proxy = ApiProxy()
        assert proxy is not None
        assert isinstance(proxy, ApiProxy)
        assert proxy._modules == {}


class TestRegisterModule:
    """Тесты регистрации модуля в прокси."""

    def test_register_module(self):
        """Регистрация модуля в прокси."""
        proxy = ApiProxy()
        module = SampleModule()

        proxy.register_module(module)

        assert "sample" in proxy._modules
        assert proxy._modules["sample"] is module


class TestApiCallThroughProxy:
    """Тесты вызова API через прокси."""

    def test_api_call_through_proxy(self):
        """Вызов state.api.sample.add(1, 2) == 3 через ApiProxy напрямую."""
        proxy = ApiProxy()
        module = SampleModule()
        proxy.register_module(module)

        # Доступ через ApiProxy
        result = proxy.sample.add(1, 2)
        assert result == 3

    def test_api_call_multiply(self):
        """Вызов proxy.sample.multiply(3, 4) == 12."""
        proxy = ApiProxy()
        module = SampleModule()
        proxy.register_module(module)

        result = proxy.sample.multiply(3, 4)
        assert result == 12

    def test_api_call_with_kwargs(self):
        """Вызов метода с именованными аргументами."""
        proxy = ApiProxy()
        module = SampleModule()
        proxy.register_module(module)

        result = proxy.sample.add(a=10, b=20)
        assert result == 30


class TestApiCallNonexistent:
    """Тесты ошибок при вызове несуществующих модулей/методов."""

    def test_api_call_nonexistent_module(self):
        """Ошибка при обращении к несуществующему модулю."""
        proxy = ApiProxy()

        with pytest.raises(AttributeError, match="Module 'ghost' not loaded"):
            _ = proxy.ghost.some_method()

    def test_api_call_nonexistent_method(self):
        """Ошибка при вызове несуществующего метода модуля."""
        proxy = ApiProxy()
        module = SampleModule()
        proxy.register_module(module)

        with pytest.raises(AttributeError, match="API method 'nonexistent' not found"):
            _ = proxy.sample.nonexistent()


class TestUnregisterModule:
    """Тесты удаления модуля из прокси."""

    def test_unregister_module(self):
        """Удаление модуля из прокси."""
        proxy = ApiProxy()
        module = SampleModule()
        proxy.register_module(module)

        assert "sample" in proxy._modules

        proxy.unregister_module("sample")

        assert "sample" not in proxy._modules

    def test_unregister_nonexistent_module(self):
        """Удаление несуществующего модуля — не ошибка."""
        proxy = ApiProxy()
        # Не должно выбросить исключение
        proxy.unregister_module("ghost")

    def test_api_after_unregister(self):
        """После удаления модуля — AttributeError при доступе к API."""
        proxy = ApiProxy()
        module = SampleModule()
        proxy.register_module(module)

        proxy.unregister_module("sample")

        with pytest.raises(AttributeError):
            _ = proxy.sample.add(1, 2)


class TestApiCallLogs:
    """Тесты логирования API вызовов."""

    @patch("communication.api_proxy.log")
    def test_api_call_logs(self, mock_log):
        """Проверка что вызов API метода логируется."""
        proxy = ApiProxy()
        module = SampleModule()
        proxy.register_module(module)

        # Вызываем API метод
        result = proxy.sample.add(5, 7)

        assert result == 12
        # Проверяем что log.info был вызван с нужными данными
        mock_log.info.assert_called_once()
        call_kwargs = mock_log.info.call_args
        assert call_kwargs[0][0] == "API call"
        extra = call_kwargs[1]["extra"]
        assert extra["module_name"] == "sample"
        assert extra["method_name"] == "add"
        assert extra["args_len"] == 2

    @patch("communication.api_proxy.log")
    def test_register_logs(self, mock_log):
        """Проверка что регистрация модуля логируется."""
        proxy = ApiProxy()
        module = SampleModule()

        proxy.register_module(module)

        mock_log.debug.assert_called_once()
        call_kwargs = mock_log.debug.call_args
        assert call_kwargs[0][0] == "Module registered in ApiProxy"
        assert call_kwargs[1]["extra"]["module_name"] == "sample"

    @patch("communication.api_proxy.log")
    def test_unregister_logs(self, mock_log):
        """Проверка что удаление модуля логируется."""
        proxy = ApiProxy()
        module = SampleModule()
        proxy.register_module(module)

        proxy.unregister_module("sample")

        mock_log.debug.assert_called()
        # Последний вызов debug — это unregister
        last_call = mock_log.debug.call_args_list[-1]
        assert last_call[0][0] == "Module unregistered from ApiProxy"
        assert last_call[1]["extra"]["module_name"] == "sample"


class TestModuleApiProxy:
    """Тесты ModuleApiProxy."""

    def test_module_api_proxy_getattr(self):
        """Получение ApiMethodProxy для существующего API метода."""
        module = SampleModule()
        proxy = ModuleApiProxy(module)

        method_proxy = proxy.add
        assert isinstance(method_proxy, ApiMethodProxy)

    def test_module_api_proxy_nonexistent_method(self):
        """AttributeError для несуществующего метода."""
        module = SampleModule()
        proxy = ModuleApiProxy(module)

        with pytest.raises(AttributeError, match="API method 'nope' not found"):
            _ = proxy.nope

    def test_module_api_proxy_non_api_method(self):
        """Обычный метод (не помеченный @api_method) — AttributeError."""
        module = SampleModule()
        proxy = ModuleApiProxy(module)

        # on_load — обычный метод, не API
        with pytest.raises(AttributeError):
            _ = proxy.on_load
