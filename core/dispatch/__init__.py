"""Диспетчер задач: очередь Redis по умолчанию, LocalInvoke для тестов."""
from core.dispatch.local import LocalInvokeDispatcher

__all__ = ["LocalInvokeDispatcher"]
