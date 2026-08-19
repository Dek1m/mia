"""Тестовый модуль — пример использования."""
from modules_system.module_base import ModuleBase, ModuleMeta, api_method

MODULE_VERSION = "1.0.0"


class SampleModule(ModuleBase):
    @property
    def name(self) -> str:
        return "sample"
    
    @property
    def version(self) -> str:
        return MODULE_VERSION
    
    @property
    def meta(self) -> ModuleMeta:
        return ModuleMeta()
    
    def __init__(self) -> None:
        self._log = None

    def on_load(self, state):
        self._log = state.log
        self._log.info("sample_module_loaded", extra={"state_type": type(state).__name__})
    
    def on_unload(self) -> None:
        self._log.info("sample_module_unloaded")
        self._log = None

    @api_method
    def add(self, a: int, b: int) -> int:
        return a + b
    
    @api_method
    def multiply(self, a: int, b: int) -> int:
        return a * b
    
    @api_method(parallel=True)
    def heavy_computation(self, data: list) -> int:
        return sum(data)