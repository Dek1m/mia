"""Полная интеграция компонентов MIA без локального пула воркеров.

Демонстрирует:
- Application: composition root
- Module lifecycle: discover → load → on_load → API → on_unload → shutdown
- ApiProxy: state.api.module.method()
- EventBus: pub/sub
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from argenta_logging import get_logger
from core.application import Application

log = get_logger(__name__)


def main() -> None:
    log.info("Integration test started")

    state = Application(modules_dir="modules")
    state.startup()
    log.info("Application started")

    log.info("Loading modules (auto-discover)")
    state.load_all_modules()
    loaded = state.modules.list_all()
    log.info("Modules loaded", extra={"modules": loaded})

    for name in state.modules.list_all():
        mod = state.modules.get(name)
        log.info("Module info", extra={"module": mod.name, "version": mod.version})

    log.info("Testing API calls via state.api")
    result_add = state.api.sample.add(1, 2)
    result_mul = state.api.sample.multiply(3, 4)
    log.info("API results", extra={"add": result_add, "multiply": result_mul})
    assert result_add == 3, f"Expected 3, got {result_add}"
    assert result_mul == 12, f"Expected 12, got {result_mul}"

    log.info("Testing EventBus pub/sub")
    received = []

    def on_data(data):
        received.append(data)

    state.event_bus.subscribe("data.processed", on_data)
    state.event_bus.publish("data.processed", {"value": 42})
    log.info("Event received", extra={"received": received})
    assert received == [{"value": 42}]

    state.event_bus.publish("unsubscribed.event", "noise")
    log.info("Publish without subscribers: OK")

    log.info("Testing module unload")
    state.unload_module("sample")
    assert "sample" not in state.modules.list_all()
    log.info("Module unloaded", extra={"module": "sample"})
    try:
        _ = state.api.sample
        log.error("Module still accessible after unload")
    except AttributeError:
        log.info("Module access after unload raises AttributeError (expected)")

    log.info("Shutting down Application")
    state.shutdown()
    log.info("Application shutdown complete")
    log.info("Integration test passed")


if __name__ == "__main__":
    main()
