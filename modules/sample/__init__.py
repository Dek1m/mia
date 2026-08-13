"""Тестовый модуль — пример использования."""
from modules_system.module_base import ModuleBase, api_method

class SampleModule(ModuleBase):
    @property
    def name(self) -> str:
        return "sample"
    
    @property
    def version(self) -> str:
        return "1.0.0"
    
    def on_load(self, state):
        print(f"SampleModule loaded! state={state}")
    
    @api_method
    def add(self, a: int, b: int) -> int:
        return a + b
    
    @api_method
    def multiply(self, a: int, b: int) -> int:
        return a * b
    
    @api_method(parallel=True)
    def heavy_computation(self, data: list) -> int:
        return sum(data)