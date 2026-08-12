"""SharedMemoryManager — управление разделяемой памятью."""
import multiprocessing.shared_memory as shm
from typing import Any
from argenta_logging import get_logger

log = get_logger(__name__)

class SharedMemoryManager:
    """Менеджер разделяемой памяти."""
    
    def __init__(self) -> None:
        self._segments: dict[str, shm.SharedMemory] = {}
    
    def create(self, name: str, size: int) -> shm.SharedMemory:
        """Создать сегмент разделяемой памяти."""
        segment = shm.SharedMemory(create=True, size=size, name=name)
        self._segments[name] = segment
        log.info("SharedMemory segment created", extra={"name": name, "size": size})
        return segment
    
    def attach(self, name: str) -> shm.SharedMemory:
        """Подключиться к существующему сегменту."""
        segment = shm.SharedMemory(name=name)
        self._segments[name] = segment
        log.info("SharedMemory segment attached", extra={"name": name})
        return segment
    
    def cleanup(self) -> None:
        """Очистить все сегменты."""
        for name, segment in self._segments.items():
            try:
                segment.close()
                segment.unlink()
            except Exception as e:
                log.error("Failed to cleanup SharedMemory", extra={"name": name, "error": str(e)})
        self._segments.clear()
        log.info("SharedMemory cleaned up")