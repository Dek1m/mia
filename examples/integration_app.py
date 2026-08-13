"""Пример: использование Application (новый API).

Запуск: python examples/integration_app.py
"""
from core.application import Application
from argenta_logging import get_logger

log = get_logger(__name__)


def main() -> None:
    app = Application(modules_dir="modules")
    app.startup()
    app.load_all_modules()

    result = app.api.sample.add(1, 2)
    log.info("API result", extra={"operation": "add", "args": [1, 2], "result": result})

    app.shutdown()


if __name__ == "__main__":
    main()
