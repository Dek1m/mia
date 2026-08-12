"""HTTP Health Check сервер."""
from __future__ import annotations

import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Callable

from argenta_logging import get_logger

log = get_logger(__name__)

# Callable проверок: имя -> () -> bool
_checkers: dict[str, Callable[[], bool]] = {}


def register_check(name: str, checker: Callable[[], bool]) -> None:
    """Зарегистрировать проверку.

    Args:
        name: Имя проверки ('ready', 'live' и т.д.).
        checker: Функция, возвращающая True если всё ОК.
    """
    _checkers[name] = checker


class _Handler(BaseHTTPRequestHandler):
    """HTTP обработчик для health check."""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Подавить стандартный лог HTTP-сервера."""
        pass

    def do_GET(self) -> None:
        """Обработать GET запрос."""
        if self.path == "/health":
            self._respond(*self._full_check())
        elif self.path == "/health/ready":
            self._respond(*self._named_check("ready"))
        elif self.path == "/health/live":
            self._respond(200, {"status": "alive"})
        else:
            self._respond(404, {"error": "not found"})

    def _full_check(self) -> tuple[int, dict[str, Any]]:
        """Полная проверка — все зарегистрированные чекеры."""
        results = {}
        healthy = True
        for name, checker in _checkers.items():
            try:
                ok = checker()
            except Exception as e:
                ok = False
                log.error("Health check failed", extra={"check": name, "error": str(e)})
            results[name] = ok
            if not ok:
                healthy = False
        code = 200 if healthy else 503
        results["status"] = "healthy" if healthy else "unhealthy"
        return code, results

    def _named_check(self, name: str) -> tuple[int, dict[str, Any]]:
        """Проверка по имени."""
        checker = _checkers.get(name)
        if checker is None:
            return 200, {"status": "ok"}
        try:
            ok = checker()
        except Exception as e:
            log.error("Health check failed", extra={"check": name, "error": str(e)})
            return 503, {"status": "error", "error": str(e)}
        code = 200 if ok else 503
        return code, {"status": "ok" if ok else "unhealthy"}

    def _respond(self, code: int, body: dict[str, Any]) -> None:
        """Отправить JSON ответ."""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())


class HealthCheckServer:
    """HTTP сервер для health check в отдельном потоке.

    Args:
        port: Порт для прослушивания.
    """

    def __init__(self, port: int = 8080) -> None:
        self._port = port
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Запустить сервер в отдельном потоке."""
        self._server = HTTPServer(("0.0.0.0", self._port), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        log.info("Health check server started", extra={"port": self._port})

    def stop(self) -> None:
        """Остановить сервер."""
        if self._server:
            self._server.shutdown()
            log.info("Health check server stopped")
