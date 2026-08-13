"""Unit-тесты для TaskClassifier — каскадная классификация задач."""
import pytest
from core.task import Task, TaskType
from core.task_classifier import (
    ClassifierRule,
    ConditionType,
    TaskClassifier,
)


class TestClassifierDefaults:
    """Дефолтные правила классификатора."""

    def test_default_rules_loaded(self):
        """Дефолтные правила загружены при инициализации."""
        clf = TaskClassifier()
        assert len(clf._rules) > 0

    def test_default_rules_sorted_by_priority(self):
        """Дефолтные правила отсортированы по убыванию приоритета."""
        clf = TaskClassifier()
        priorities = [r.priority for r in clf._rules]
        assert priorities == sorted(priorities, reverse=True)


class TestCascadeLevel1FnTaskType:
    """Уровень 1: явный атрибут fn._task_type."""

    def test_explicit_task_type_string(self):
        """_task_type как строка → TaskType."""
        clf = TaskClassifier()

        def fn(): pass
        fn._task_type = "cpu"

        task = Task.create(module_id="x", fn_name="f")
        assert clf.classify(task, fn) == TaskType.CPU

    def test_explicit_task_type_enum(self):
        """_task_type как TaskType → TaskType."""
        clf = TaskClassifier()

        def fn(): pass
        fn._task_type = TaskType.GPU

        task = Task.create(module_id="x", fn_name="f")
        assert clf.classify(task, fn) == TaskType.GPU

    def test_explicit_task_type_invalid_string(self):
        """_task_type с невалидной строкой → UNKNOWN."""
        clf = TaskClassifier()

        def fn(): pass
        fn._task_type = "bogus"

        task = Task.create(module_id="x", fn_name="f")
        assert clf.classify(task, fn) == TaskType.UNKNOWN


class TestCascadeLevel2FnDbType:
    """Уровень 2: явный атрибут fn._db_type (обратная совместимость)."""

    def test_db_type_string(self):
        """_db_type как строка → TaskType."""
        clf = TaskClassifier()

        def fn(): pass
        fn._db_type = "database"

        task = Task.create(module_id="x", fn_name="f")
        assert clf.classify(task, fn) == TaskType.DATABASE

    def test_db_type_overridden_by_task_type(self):
        """_task_type приоритетнее _db_type."""
        clf = TaskClassifier()

        def fn(): pass
        fn._task_type = "io"
        fn._db_type = "database"

        task = Task.create(module_id="x", fn_name="f")
        assert clf.classify(task, fn) == TaskType.IO


class TestCascadeLevel3ModuleName:
    """Уровень 3: имя модуля (module_id)."""

    @pytest.mark.parametrize(
        "module,expected",
        [
            ("db", TaskType.DATABASE),
            ("database", TaskType.DATABASE),
            ("sql", TaskType.DATABASE),
            ("math", TaskType.CPU),
            ("compute", TaskType.CPU),
            ("cpu", TaskType.CPU),
            ("graphics", TaskType.GPU),
            ("gpu", TaskType.GPU),
            ("render", TaskType.GPU),
            ("network", TaskType.NETWORK),
            ("http", TaskType.NETWORK),
            ("api", TaskType.NETWORK),
        ],
    )
    def test_module_name_to_type(self, module, expected):
        """Имя модуля маппится на TaskType."""
        clf = TaskClassifier()
        task = Task.create(module_id=module, fn_name="f")
        assert clf.classify(task) == expected

    def test_module_name_case_insensitive(self):
        """Имя модуля регистронезависимо."""
        clf = TaskClassifier()
        task = Task.create(module_id="DB", fn_name="f")
        assert clf.classify(task) == TaskType.DATABASE


class TestCascadeLevel4FnName:
    """Уровень 4: имя функции (heuristic)."""

    @pytest.mark.parametrize(
        "fn_name,expected",
        [
            ("get_user", TaskType.IO),
            ("select_all", TaskType.IO),
            ("read_file", TaskType.IO),
            ("insert_row", TaskType.IO),
            ("update_record", TaskType.IO),
            ("delete_item", TaskType.IO),
            ("write_data", TaskType.IO),
            ("compute_hash", TaskType.CPU),
            ("calculate_sum", TaskType.CPU),
            ("process_batch", TaskType.CPU),
            ("render_frame", TaskType.GPU),
            ("draw_circle", TaskType.GPU),
            ("transform_image", TaskType.GPU),
            ("fetch_url", TaskType.NETWORK),
            ("request_api", TaskType.NETWORK),
            ("http_get", TaskType.NETWORK),
        ],
    )
    def test_fn_name_pattern(self, fn_name, expected):
        """Имя функции маппится на TaskType по паттерну."""
        clf = TaskClassifier()
        task = Task.create(module_id="other", fn_name=fn_name)
        assert clf.classify(task) == expected


class TestCascadeLevel5Fallback:
    """Уровень 5: fallback → UNKNOWN."""

    def test_fallback_no_fn(self):
        """Без fn и без совпадений → UNKNOWN."""
        clf = TaskClassifier()
        task = Task.create(module_id="unknown_module", fn_name="random_func")
        assert clf.classify(task) == TaskType.UNKNOWN

    def test_fallback_with_fn_no_attrs(self):
        """fn без атрибутов и без совпадений → UNKNOWN."""
        clf = TaskClassifier()

        def fn(): pass

        task = Task.create(module_id="other", fn_name="random_func")
        assert clf.classify(task, fn) == TaskType.UNKNOWN


class TestPriorityOverRules:
    """Приоритеты правил: модуль > имя функции."""

    def test_module_priority_over_fn_name(self):
        """Правило модуля (priority=100) выше правила имени (priority=50)."""
        clf = TaskClassifier()
        # get_user → IO по имени функции, но модуль "db" → DATABASE
        task = Task.create(module_id="db", fn_name="get_user")
        assert clf.classify(task) == TaskType.DATABASE

    def test_explicit_attr_priority_over_module(self):
        """Явный атрибут (уровень 1) выше модуля (уровень 3)."""
        clf = TaskClassifier()

        def fn(): pass
        fn._task_type = "cpu"

        # Модуль "db" → DATABASE, но _task_type → CPU
        task = Task.create(module_id="db", fn_name="get_user")
        assert clf.classify(task, fn) == TaskType.CPU


class TestAddRule:
    """Добавление правил."""

    def test_add_rule_basic(self):
        """add_rule() добавляет правило."""
        clf = TaskClassifier()
        initial_count = len(clf._rules)
        clf.add_rule(
            priority=200,
            condition_type=ConditionType.MODULE_NAME,
            condition_value="custom",
            target_type=TaskType.GPU,
        )
        assert len(clf._rules) == initial_count + 1

    def test_add_rule_high_priority_takes_effect(self):
        """Правило с высоким приоритетом перебивает дефолтные."""
        clf = TaskClassifier()
        clf.add_rule(
            priority=999,
            condition_type=ConditionType.MODULE_NAME,
            condition_value="db",
            target_type=TaskType.CPU,
        )
        task = Task.create(module_id="db", fn_name="query")
        assert clf.classify(task) == TaskType.CPU

    def test_add_rule_invalid_priority_raises(self):
        """Отрицательный приоритет → ValueError."""
        clf = TaskClassifier()
        with pytest.raises(ValueError):
            clf.add_rule(
                priority=-1,
                condition_type=ConditionType.MODULE_NAME,
                condition_value="x",
                target_type=TaskType.IO,
            )

    def test_rules_stay_sorted_after_add(self):
        """Правила остаются отсортированными после добавления."""
        clf = TaskClassifier()
        clf.add_rule(
            priority=75,
            condition_type=ConditionType.FN_NAME_PATTERN,
            condition_value=r"^custom_.*",
            target_type=TaskType.GPU,
        )
        priorities = [r.priority for r in clf._rules]
        assert priorities == sorted(priorities, reverse=True)


class TestLoadRulesFromDB:
    """Загрузка правил из БД."""

    def test_load_valid_rules(self):
        """load_rules_from_db() загружает валидные правила."""
        clf = TaskClassifier()
        initial_count = len(clf._rules)
        rules = [
            {
                "priority": 150,
                "condition_type": "module_name",
                "condition_value": "redis",
                "target_type": "io",
            },
            {
                "priority": 120,
                "condition_type": "fn_name_pattern",
                "condition_value": r"^cache_.*",
                "target_type": "io",
            },
        ]
        clf.load_rules_from_db(rules)
        assert len(clf._rules) == initial_count + 2

    def test_load_rules_invalid_condition_type(self):
        """Правила с невалидным condition_type пропускаются."""
        clf = TaskClassifier()
        initial_count = len(clf._rules)
        rules = [
            {
                "priority": 100,
                "condition_type": "bogus",
                "condition_value": "x",
                "target_type": "io",
            },
        ]
        clf.load_rules_from_db(rules)
        assert len(clf._rules) == initial_count

    def test_load_rules_invalid_target_type(self):
        """Правила с невалидным target_type пропускаются."""
        clf = TaskClassifier()
        initial_count = len(clf._rules)
        rules = [
            {
                "priority": 100,
                "condition_type": "module_name",
                "condition_value": "x",
                "target_type": "bogus",
            },
        ]
        clf.load_rules_from_db(rules)
        assert len(clf._rules) == initial_count

    def test_load_rules_invalid_priority(self):
        """Правила с невалидным приоритетом пропускаются."""
        clf = TaskClassifier()
        initial_count = len(clf._rules)
        rules = [
            {
                "priority": -5,
                "condition_type": "module_name",
                "condition_value": "x",
                "target_type": "io",
            },
            {
                "priority": "abc",
                "condition_type": "module_name",
                "condition_value": "y",
                "target_type": "io",
            },
        ]
        clf.load_rules_from_db(rules)
        assert len(clf._rules) == initial_count

    def test_loaded_rules_respect_priority(self):
        """Загруженные правила участвуют в каскаде по приоритету."""
        clf = TaskClassifier()
        clf.load_rules_from_db(
            [
                {
                    "priority": 999,
                    "condition_type": "module_name",
                    "condition_value": "db",
                    "target_type": "gpu",
                },
            ]
        )
        task = Task.create(module_id="db", fn_name="get_user")
        assert clf.classify(task) == TaskType.GPU


class TestClassifierRuleDataclass:
    """ClassifierRule dataclass."""

    def test_frozen(self):
        """ClassifierRule неизменяем."""
        rule = ClassifierRule(
            priority=100,
            condition_type=ConditionType.MODULE_NAME,
            condition_value="db",
            target_type=TaskType.DATABASE,
        )
        with pytest.raises(AttributeError):
            rule.priority = 200  # type: ignore[misc]

    def test_invalid_priority_value_error(self):
        """ClassifierRule с отрицательным приоритетом → ValueError."""
        with pytest.raises(ValueError):
            ClassifierRule(
                priority=-1,
                condition_type=ConditionType.MODULE_NAME,
                condition_value="x",
                target_type=TaskType.IO,
            )
