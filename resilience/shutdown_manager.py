"""ShutdownManager — корректное завершение с таймаутом."""
from __future__ import annotations

import signal
import threading
from typing import Callable, Any
from argenta_logging import get_logger
from core.interfaces import IShutdownManager

log = get_logger(__name__)


class ShutdownManager(IShutdownManager):
    """Менеджер корректного завершения."""
    
    def __init__(self, timeout: float | None = None) -> None:
        if timeout is None:
            from core.config import MiaConfig
            timeout = MiaConfig.get().get_value("core.shutdown.timeout", 30.0)
        self._timeout = timeout
        self._hooks: list[Callable] = []
        self._lock = threading.Lock()
        self._shutdown_event = threading.Event()
        
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
    
    def _handle_signal(self, signum: int, frame: Any) -> None:
        log.info("Shutdown signal received", extra={"signal": signum})
        self._shutdown_event.set()
    
    def register_hook(self, hook: Callable) -> None:
        with self._lock:
            self._hooks.append(hook)
    
    def shutdown(self, timeout: float | None = None) -> None:
        timeout = timeout or self._timeout
        log.info("Executing graceful shutdown", extra={"hooks": len(self._hooks)})
        
        with self._lock:
            hooks = self._hooks.copy()
        
        for hook in hooks:
            try:
                thread = threading.Thread(target=hook, daemon=True)
                thread.start()
                thread.join(timeout=timeout / max(len(hooks), 1))
                if thread.is_alive():
                    log.warning("Shutdown hook timeout", extra={"hook": hook.__name__})
            except Exception as e:
                log.error("Shutdown hook error", extra={"hook": hook.__name__, "error": str(e)})
        
        log.info("Graceful shutdown complete")
    
    def wait_for_signal(self) -> None:
        self._shutdown_event.wait()
