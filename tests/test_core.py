"""Unit-тесты для core компонентов: State, ModuleManager, ModuleBase."""
import pytest
from core.application import Application
from modules_system.module_base import ModuleBase, api_method
from modules.sample import SampleModule
from core.errors import ModuleLoadError


def test_state_creation():
    """Application() создаётся без ошибок."""
    state = Application()
    assert state is not None


def test_load_sample_module():
    """Загрузка модуля sample."""
    state = Application(modules_dir="modules")
    state.load_module("sample")
    
    # Проверяем, что модуль загружен
    assert "sample" in state.modules.list_all()
    assert state.modules.get("sample").name == "sample"
    assert state.modules.get("sample").version == "1.0.0"


def test_api_call():
    """Вызов state.api.sample.add(1, 2) == 3."""
    state = Application(modules_dir="modules")
    state.load_module("sample")
    
    # Вызов API метода
    result = state.api.sample.add(1, 2)
    assert result == 3


def test_api_multiply():
    """Вызов state.api.sample.multiply(3, 4) == 12."""
    state = Application(modules_dir="modules")
    state.load_module("sample")
    
    # Вызов API метода
    result = state.api.sample.multiply(3, 4)
    assert result == 12


def test_unload_module():
    """Выгрузка модуля."""
    state = Application(modules_dir="modules")
    state.load_module("sample")
    
    # Выгружаем
    state.unload_module("sample")
    
    # Проверяем, что модуль выгружен
    assert "sample" not in state.modules.list_all()
    
    # Проверяем, что API метод больше не доступен
    with pytest.raises(AttributeError):
        _ = state.api.sample


def test_load_nonexistent_module():
    """Ошибка при загрузке несуществующего модуля."""
    state = Application(modules_dir="modules")
    
    # Попытка загрузить несуществующий модуль
    with pytest.raises(ModuleLoadError):
        state.load_module("nonexistent_module")


def test_shutdown():
    """Shutdown выгружает все модули."""
    state = Application(modules_dir="modules")
    
    # Загружаем несколько модулей (пока только sample)
    state.load_module("sample")
    assert len(state.modules.list_all()) == 1
    
    # Вызываем shutdown
    state.shutdown()
    
    # Проверяем, что все модули выгружены
    assert len(state.modules.list_all()) == 0


def test_api_method_decorator():
    """Декоратор api_method помечает методы корректно."""
    # Проверяем, что методы имеют атрибуты _is_api_method и _parallel
    assert hasattr(SampleModule.add, '_is_api_method')
    assert SampleModule.add._is_api_method is True
    assert hasattr(SampleModule.multiply, '_is_api_method')
    assert SampleModule.multiply._is_api_method is True
    
    # Проверяем parallel
    assert hasattr(SampleModule.heavy_computation, '_parallel')
    assert SampleModule.heavy_computation._parallel is True


def test_api_nonexistent_method():
    """Попытка вызова несуществующего API метода."""
    state = Application(modules_dir="modules")
    state.load_module("sample")
    
    # Попытка вызвать несуществующий метод
    with pytest.raises(AttributeError):
        _ = state.api.sample.nonexistent_method()


def test_api_nonexistent_module():
    """Попытка доступа к API несуществующего модуля."""
    state = Application(modules_dir="modules")
    
    # Попытка доступа к API несуществующего модуля
    with pytest.raises(AttributeError):
        _ = state.api.nonexistent_module.some_method()


def test_load_already_loaded():
    """Повторная загрузка уже загруженного модуля."""
    state = Application(modules_dir="modules")
    state.load_module("sample")
    
    # Повторная загрузка не должна вызывать ошибку
    state.load_module("sample")  # Должно просто вернуться
    
    # Проверяем, что модуль всё ещё загружен
    assert "sample" in state.modules.list_all()


def test_module_base_version():
    """Проверка версии модуля по умолчанию."""
    # Создаем простой модуль без переопределения version
    class SimpleModule(ModuleBase):
        @property
        def name(self) -> str:
            return "simple"
    
    module = SimpleModule()
    assert module.version == "0.0.0"


def test_module_on_load():
    """Проверка вызова on_load."""
    state = Application(modules_dir="modules")
    state.load_module("sample")
    
    # on_load уже был вызван при загрузке
    # Проверяем, что модуль корректно инициализирован
    module = state.modules.get("sample")
    assert module.name == "sample"