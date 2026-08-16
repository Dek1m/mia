"""Тестовый модуль — пример использования."""
from modules_system.module_base import ModuleBase, api_method
from argenta_logging import get_logger

log = get_logger(__name__)

MODULE_VERSION = "1.0.0"


class SampleModule(ModuleBase):
    @property
    def name(self) -> str:
        return "sample"
    
    @property
    def version(self) -> str:
        return MODULE_VERSION
    
    def on_load(self, state):
        log.info("sample_module_loaded", extra={"state_type": type(state).__name__})
    
    @api_method
    def add(self, a: int, b: int) -> int:
        return a + b
    
    @api_method
    def multiply(self, a: int, b: int) -> int:
        return a * b
    
    @api_method(parallel=True)
    def heavy_computation(self, data: list) -> int:
        return sum(data)