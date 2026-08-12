"""CpuAffinityProvider — привязка процессов к ядрам CPU."""
import os
import platform
from argenta_logging import get_logger

log = get_logger(__name__)

class CpuAffinityProvider:
    """Провайдер CPU affinity с поддержкой Linux и Windows.
    
    На Linux использует os.sched_setaffinity.
    На Windows использует psutil (если доступен).
    При ошибке — graceful degradation (продолжает без привязки).
    """
    
    def __init__(self) -> None:
        self._available = self._check_availability()
        log.info("CpuAffinityProvider created", extra={
            "platform": platform.system(),
            "available": self._available,
        })
    
    def _check_availability(self) -> bool:
        """Проверить доступность affinity на текущей платформе."""
        if platform.system() == "Linux":
            return hasattr(os, "sched_setaffinity")
        elif platform.system() == "Windows":
            try:
                import psutil
                return True
            except ImportError:
                return False
        return False
    
    def get_cpu_count(self) -> int:
        """Получить количество доступных ядер."""
        return os.cpu_count() or 1
    
    def set_affinity(self, pid: int, cores: set[int]) -> bool:
        """Установить привязку процесса к ядрам.
        
        Args:
            pid: ID процесса (0 = текущий).
            cores: Множество номеров ядер.
        
        Returns:
            True если привязка успешна, False если не удалась.
        """
        if not self._available:
            log.warning("CPU affinity not available", extra={"platform": platform.system()})
            return False
        
        try:
            if platform.system() == "Linux":
                os.sched_setaffinity(pid, cores)
                log.info("CPU affinity set (Linux)", extra={"pid": pid, "cores": list(cores)})
                return True
            elif platform.system() == "Windows":
                import psutil
                p = psutil.Process(pid)
                p.cpu_affinity(list(cores))
                log.info("CPU affinity set (Windows)", extra={"pid": pid, "cores": list(cores)})
                return True
        except Exception as e:
            log.error("Failed to set CPU affinity", extra={"pid": pid, "cores": list(cores), "error": str(e)})
            return False
    
    def get_affinity(self, pid: int) -> set[int] | None:
        """Получить привязку процесса к ядрам."""
        if not self._available:
            return None
        try:
            if platform.system() == "Linux":
                return os.sched_getaffinity(pid)
            elif platform.system() == "Windows":
                import psutil
                p = psutil.Process(pid)
                return set(p.cpu_affinity())
        except Exception as e:
            log.error("Failed to get CPU affinity", extra={"pid": pid, "error": str(e)})
            return None