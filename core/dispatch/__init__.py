"""Диспетчер задач: Shaltir по умолчанию, LocalInvoke для тестов."""
from core.dispatch.local import LocalInvokeDispatcher
from core.dispatch.shaltir import ShaltirDispatcher

__all__ = ["LocalInvokeDispatcher", "ShaltirDispatcher"]
