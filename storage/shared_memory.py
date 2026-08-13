"""SharedMemoryManager — управление разделяемой памятью."""
import multiprocessing.shared_memory as shm
import threading
from typing import Any
from argenta_logging import get_logger

log = get_logger(__name__)

class SharedMemoryManager:
    """Менеджер разделяемой памяти."""
    
    def __init__(self) -> None:
        self._segments: dict[str, shm.SharedMemory] = {}
        self._lock = threading.Lock()
    
    def create(self, name: str, size: int) -> shm.SharedMemory:
        """Создать сегмент разделяемой памяти."""
        segment = shm.SharedMemory(create=True, size=size, name=name)
        with self._lock:
            self._segments[name] = segment
        log.info("SharedMemory segment created", extra={"name": name, "size": size})
        return segment
    
    def attach(self, name: str) -> shm.SharedMemory:
        """Подключиться к существующему сегменту."""
        segment = shm.SharedMemory(name=name)
        with self._lock:
            self._segments[name] = segment
        log.info("SharedMemory segment attached", extra={"name": name})
        return segment
    
    def cleanup(self) -> None:
        """Очистить все сегменты."""
        with self._lock:
            segments = dict(self._segments)
            self._segments.clear()
        for name, segment in segments.items():
            try:
                segment.close()
                segment.unlink()
            except Exception as e:
                log.error("Failed to cleanup SharedMemory", extra={"name": name, "error": str(e)})
        log.info("SharedMemory cleaned up")
