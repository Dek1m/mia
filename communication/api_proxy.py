"""API Proxy — динамический доступ к API модулей."""
from typing import Any, Callable
import time

from argenta_logging import get_logger
from monitoring.metrics import api_calls_total, api_duration_seconds

log = get_logger(__name__)


class ApiMethodProxy:
    """Прокси для одного метода модуля."""

    def __init__(self, method: Callable, module_name: str, method_name: str, dispatcher: Any | None = None) -> None:
        self._method = method
        self._module_name = module_name
        self._method_name = method_name
        self._dispatcher = dispatcher

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        log.info("API call", extra={
            "module_name": self._module_name,
            "method_name": self._method_name,
            "args_len": len(args),
        })
        api_calls_total.labels(module=self._module_name, method=self._method_name, status="ok").inc()
        start = time.monotonic()
        try:
            if getattr(self._method, "_parallel", False) and self._dispatcher is not None:
                # parallel=True: dispatch через SmartDispatcher (WorkerManager)
                result = self._dispatcher.dispatch_async(self._method, *args, **kwargs)
            else:
                result = self._method(*args, **kwargs)
            return result
        except Exception as e:
            api_calls_total.labels(module=self._module_name, method=self._method_name, status="error").inc()
            raise
        finally:
            duration = time.monotonic() - start
            api_duration_seconds.labels(module=self._module_name, method=self._method_name).observe(duration)


class ModuleApiProxy:
    """Прокси для API одного модуля."""

    def __init__(self, module: "ModuleBase", dispatcher: Any | None = None) -> None:  # noqa: F821
        self._module = module
        self._module_name = module.name
        self._dispatcher = dispatcher

    def __getattr__(self, name: str) -> ApiMethodProxy:
        attr = getattr(self._module, name, None)
        if attr is not None and getattr(attr, "_is_api_method", False):
            return ApiMethodProxy(attr, self._module_name, name, self._dispatcher)
        raise AttributeError(
            f"API method '{name}' not found in module '{self._module_name}'"
        )


class ApiProxy:
    """Главный прокси для доступа к API всех модулей."""

    def __init__(self, dispatcher: Any | None = None) -> None:
        self._modules: dict[str, "ModuleBase"] = {}  # noqa: F821
        self._dispatcher = dispatcher

    def register_module(self, module: "ModuleBase") -> None:  # noqa: F821
        """Зарегистрировать модуль в прокси."""
        self._modules[module.name] = module
        log.debug("Module registered in ApiProxy", extra={"module_name": module.name})

    def unregister_module(self, name: str) -> None:
        """Убрать модуль из прокси."""
        self._modules.pop(name, None)
        log.debug("Module unregistered from ApiProxy", extra={"module_name": name})

    def __getattr__(self, name: str) -> ModuleApiProxy:
        if name in self._modules:
            return ModuleApiProxy(self._modules[name], self._dispatcher)
        raise AttributeError(f"Module '{name}' not loaded")
