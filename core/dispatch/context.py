"""Correlation id запроса: транспорт ставит, QueueDispatcher читает.

ContextVar вместо параметра dispatch: публичные сигнатуры @task-методов
и ISmartDispatcher не меняются, а request_id проходит REST → envelope → worker.
"""
from __future__ import annotations

from contextvars import ContextVar

__all__ = ["request_id_var"]

request_id_var: ContextVar[str | None] = ContextVar("mia_request_id", default=None)
