"""CpuAffinityProvider — привязка процессов к ядрам CPU (Linux only)."""
import os
from argenta_logging import get_logger
from monitoring.metrics import cpu_affinity_set_total, cpu_affinity_errors_total

log = get_logger(__name__)


class CpuAffinityProvider:
    """CPU affinity для Linux."""

    def __init__(self) -> None:
        self._cpu_count = os.cpu_count() or 1

    def get_cpu_count(self) -> int:
        return self._cpu_count

    def set_affinity(self, pid: int, cores: set[int]) -> bool:
        """Привязать процесс к ядрам CPU.

        Args:
            pid: ID процесса (0 = текущий).
            cores: Множество ID ядер.

        Returns:
            True если привязка успешна.
        """
        try:
            os.sched_setaffinity(pid, cores)
            cpu_affinity_set_total.inc()
            log.info("CPU affinity set", extra={"pid": pid, "cores": sorted(cores)})
            return True
        except OSError as e:
            cpu_affinity_errors_total.inc()
            log.warning("CPU affinity error", extra={"pid": pid, "cores": sorted(cores), "error": str(e)})
            return False

    def get_affinity(self, pid: int) -> set[int]:
        """Получить текущую привязку процесса к ядрам.

        Args:
            pid: ID процесса (0 = текущий).

        Returns:
            Множество ID ядер.
        """
        affinity = os.sched_getaffinity(pid)
        log.debug("CPU affinity retrieved", extra={"pid": pid, "cores": sorted(affinity)})
        return affinity
