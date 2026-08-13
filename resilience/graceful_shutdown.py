"""Graceful Shutdown — корректное завершение с таймаутами."""
from __future__ import annotations

import signal
import threading
from typing import Callable

from argenta_logging import get_logger

log = get_logger(__name__)

# Хуки завершения: () -> None
_hooks: list[Callable[[], None]] = []
_default_timeout: float = 10.0
_registered = False


def register_hook(hook: Callable[[], None]) -> None:
    """Зарегистрировать хук завершения.

    Args:
        hook: Функция без аргументов, вызываемая при завершении.
    """
    _hooks.append(hook)
    log.debug("Shutdown hook registered", extra={"hook": hook.__name__})


def set_timeout(timeout: float) -> None:
    """Установить таймаут на все хуки.

    Args:
        timeout: Максимальное время на один хук в секундах.
    """
    global _default_timeout
    _default_timeout = timeout


def _run_hooks() -> None:
    """Выполнить все хуки с таймаутами."""
    log.info("Running shutdown hooks", extra={"count": len(_hooks), "timeout": _default_timeout})

    for hook in _hooks:
        event = threading.Event()

        def _target(h: Callable[[], None] = hook) -> None:
            try:
                h()
            except Exception as e:
                log.error("Shutdown hook failed", extra={"hook": h.__name__, "error": str(e)})
            finally:
                event.set()

        thread = threading.Thread(target=_target, daemon=True)
        thread.start()

        if not event.wait(timeout=_default_timeout):
            log.error(
                "Shutdown hook timed out",
                extra={"hook": hook.__name__, "timeout": _default_timeout},
            )
            from core.errors import ShutdownTimeoutError

            raise ShutdownTimeoutError(f"Hook '{hook.__name__}' timed out after {_default_timeout}s")


def _signal_handler(signum: int, _frame: object) -> None:
    """Обработчик сигналов SIGTERM/SIGINT."""
    sig_name = signal.Signals(signum).name
    log.info("Received signal", extra={"signal": sig_name})
    _run_hooks()


def install_signal_handlers() -> None:
    """Установить обработчики SIGTERM и SIGINT."""
    global _registered
    if _registered:
        return

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)
    _registered = True
    log.info("Signal handlers installed")
