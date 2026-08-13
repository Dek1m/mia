"""Пример: использование Application (новый API).

Запуск: python examples/integration_app.py
"""
from core.application import Application


def main() -> None:
    app = Application(modules_dir="modules")
    app.startup()
    app.load_all_modules()

    result = app.api.sample.add(1, 2)
    print(f"1 + 2 = {result}")

    app.shutdown()


if __name__ == "__main__":
    main()
