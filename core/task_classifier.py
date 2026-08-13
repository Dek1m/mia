"""TaskClassifier — каскадная классификация задач по типам."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from core.task import Task, TaskType

# Прямой импорт метрик
try:
    from monitoring.metrics import task_classifier_rules_total as _rules_gauge
except ImportError:
    _rules_gauge = None


class ConditionType(Enum):
    """Тип условия правила классификации."""

    FN_ATTRIBUTE = "fn_attribute"
    MODULE_NAME = "module_name"
    FN_NAME_PATTERN = "fn_name_pattern"


@dataclass(frozen=True)
class ClassifierRule:
    """Правило классификатора."""

    priority: int
    condition_type: ConditionType
    condition_value: str
    target_type: TaskType

    def __post_init__(self) -> None:
        if self.priority < 0:
            raise ValueError(f"priority must be >= 0, got {self.priority}")


@dataclass
class TaskClassifier:
    """Каскадный классификатор задач.

    Определяет TaskType по правилам приоритетного каскада:
    1. Явный атрибут fn._task_type
    2. Явный атрибут fn._db_type (обратная совместимость)
    3. Имя модуля (module_id)
    4. Имя функции (heuristic)
    5. Fallback → UNKNOWN
    """

    _rules: list[ClassifierRule] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self._rules:
            self._rules = self._default_rules()

    @staticmethod
    def _default_rules() -> list[ClassifierRule]:
        """Дефолтный набор правил (каскад из плана)."""
        rules = []

        # Уровень 3: имя модуля
        module_map = {
            "db": TaskType.DATABASE,
            "database": TaskType.DATABASE,
            "sql": TaskType.DATABASE,
            "math": TaskType.CPU,
            "compute": TaskType.CPU,
            "cpu": TaskType.CPU,
            "graphics": TaskType.GPU,
            "gpu": TaskType.GPU,
            "render": TaskType.GPU,
            "network": TaskType.NETWORK,
            "http": TaskType.NETWORK,
            "api": TaskType.NETWORK,
        }
        for name, target in module_map.items():
            rules.append(
                ClassifierRule(
                    priority=100,
                    condition_type=ConditionType.MODULE_NAME,
                    condition_value=name,
                    target_type=target,
                )
            )

        # Уровень 4: имя функции
        fn_patterns: list[tuple[str, TaskType]] = [
            (r"^get_.*", TaskType.IO),
            (r"^select_.*", TaskType.IO),
            (r"^read_.*", TaskType.IO),
            (r"^insert_.*", TaskType.IO),
            (r"^update_.*", TaskType.IO),
            (r"^delete_.*", TaskType.IO),
            (r"^write_.*", TaskType.IO),
            (r"^compute_.*", TaskType.CPU),
            (r"^calculate_.*", TaskType.CPU),
            (r"^process_.*", TaskType.CPU),
            (r"^render_.*", TaskType.GPU),
            (r"^draw_.*", TaskType.GPU),
            (r"^transform_.*", TaskType.GPU),
            (r"^fetch_.*", TaskType.NETWORK),
            (r"^request_.*", TaskType.NETWORK),
            (r"^http_.*", TaskType.NETWORK),
        ]
        for pattern, target in fn_patterns:
            rules.append(
                ClassifierRule(
                    priority=50,
                    condition_type=ConditionType.FN_NAME_PATTERN,
                    condition_value=pattern,
                    target_type=target,
                )
            )

        rules.sort(key=lambda r: r.priority, reverse=True)
        return rules

    def classify(self, task: Task, fn: Callable | None = None) -> TaskType:
        """Определить тип задачи по каскаду правил.

        Каскад (приоритет от высшего к низшему):
        1. fn._task_type → TaskType
        2. fn._db_type → TaskType (обратная совместимость)
        3. module_id → TaskType
        4. fn_name → TaskType (heuristic)
        5. Fallback → UNKNOWN
        """
        # Уровень 1: явный атрибут _task_type
        if fn is not None:
            attr = getattr(fn, "_task_type", None)
            if attr is not None:
                return self._resolve_task_type(attr)

        # Уровень 2: явный атрибут _db_type (обратная совместимость)
        if fn is not None:
            attr = getattr(fn, "_db_type", None)
            if attr is not None:
                return self._resolve_task_type(attr)

        # Уровни 3-4: пользовательские правила
        for rule in self._rules:
            if self._match_rule(rule, task):
                return rule.target_type

        # Уровень 5: fallback
        return TaskType.UNKNOWN

    def _resolve_task_type(self, value: Any) -> TaskType:
        """Преобразовать значение атрибута в TaskType."""
        if isinstance(value, TaskType):
            return value
        if isinstance(value, str):
            try:
                return TaskType(value)
            except ValueError:
                pass
        return TaskType.UNKNOWN

    def _match_rule(self, rule: ClassifierRule, task: Task) -> bool:
        """Проверить, соответствует ли задача правилу."""
        if rule.condition_type == ConditionType.MODULE_NAME:
            return task.module_id.lower() == rule.condition_value.lower()

        if rule.condition_type == ConditionType.FN_NAME_PATTERN:
            return bool(re.match(rule.condition_value, task.fn_name))

        return False

    def add_rule(
        self,
        priority: int,
        condition_type: ConditionType,
        condition_value: str,
        target_type: TaskType,
    ) -> None:
        """Добавить правило классификации."""
        rule = ClassifierRule(
            priority=priority,
            condition_type=condition_type,
            condition_value=condition_value,
            target_type=target_type,
        )
        self._rules.append(rule)
        self._rules.sort(key=lambda r: r.priority, reverse=True)
        if _rules_gauge is not None:
            _rules_gauge.set(len(self._rules))

    def load_rules_from_db(self, rules: list[dict[str, Any]]) -> None:
        """Загрузить правила из БД (список dict-ов).

        Ожидаемые ключи: priority, condition_type, condition_value, target_type.
        """
        for raw in rules:
            ct_str = raw.get("condition_type", "")
            try:
                ct = ConditionType(ct_str)
            except ValueError:
                continue
            tt_str = raw.get("target_type", "")
            try:
                tt = TaskType(tt_str)
            except ValueError:
                continue
            priority = raw.get("priority", 0)
            if not isinstance(priority, int) or priority < 0:
                continue
            self._rules.append(
                ClassifierRule(
                    priority=priority,
                    condition_type=ct,
                    condition_value=raw.get("condition_value", ""),
                    target_type=tt,
                )
            )
        self._rules.sort(key=lambda r: r.priority, reverse=True)
        if _rules_gauge is not None:
            _rules_gauge.set(len(self._rules))
