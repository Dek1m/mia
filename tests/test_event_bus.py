"""Unit-тесты для EventBus."""
import pytest
from unittest.mock import patch

from communication.event_bus import EventBus


class TestEventBusCreation:
    """Тесты создания EventBus."""

    def test_event_bus_creation(self):
        """EventBus() создаётся без ошибок."""
        bus = EventBus()
        assert bus is not None
        assert isinstance(bus, EventBus)
        assert bus._subscribers == {}


class TestSubscribeAndPublish:
    """Тесты подписки и публикации."""

    @patch("communication.event_bus.log")
    def test_subscribe_and_publish(self, _mock_log):
        """Подписка на событие + публикация — handler вызывается."""
        bus = EventBus()
        calls = []

        def handler(data):
            calls.append(data)

        bus.subscribe("test.event", handler)
        bus.publish("test.event", data="hello")

        assert calls == ["hello"]

    @patch("communication.event_bus.log")
    def test_publish_with_none_data(self, _mock_log):
        """Публикация без data — handler получает None."""
        bus = EventBus()
        calls = []

        def handler(data):
            calls.append(data)

        bus.subscribe("event", handler)
        bus.publish("event")

        assert calls == [None]

    @patch("communication.event_bus.log")
    def test_publish_with_complex_data(self, _mock_log):
        """Публикация со сложным объектом."""
        bus = EventBus()
        calls = []

        def handler(data):
            calls.append(data)

        bus.subscribe("event", handler)
        payload = {"key": "value", "nested": [1, 2, 3]}
        bus.publish("event", data=payload)

        assert calls == [payload]


class TestMultipleSubscribers:
    """Тесты нескольких подписчиков."""

    @patch("communication.event_bus.log")
    def test_multiple_subscribers(self, _mock_log):
        """Несколько подписчиков на одно событие — все вызываются."""
        bus = EventBus()
        calls = []

        def handler1(data):
            calls.append(("h1", data))

        def handler2(data):
            calls.append(("h2", data))

        def handler3(data):
            calls.append(("h3", data))

        bus.subscribe("event", handler1)
        bus.subscribe("event", handler2)
        bus.subscribe("event", handler3)

        bus.publish("event", data="test")

        assert calls == [("h1", "test"), ("h2", "test"), ("h3", "test")]

    @patch("communication.event_bus.log")
    def test_subscribers_called_in_order(self, _mock_log):
        """Подписчики вызываются в порядке подписки."""
        bus = EventBus()
        call_order = []

        def handler_a(data):
            call_order.append("a")

        def handler_b(data):
            call_order.append("b")

        bus.subscribe("event", handler_a)
        bus.subscribe("event", handler_b)

        bus.publish("event")

        assert call_order == ["a", "b"]


class TestUnsubscribe:
    """Тесты отписки."""

    @patch("communication.event_bus.log")
    def test_unsubscribe(self, _mock_log):
        """Отписка от события — handler больше не вызывается."""
        bus = EventBus()
        calls = []

        def handler(data):
            calls.append(data)

        bus.subscribe("event", handler)
        bus.unsubscribe("event", handler)
        bus.publish("event", data="test")

        assert calls == []

    @patch("communication.event_bus.log")
    def test_unsubscribe_only_target(self, _mock_log):
        """Отписка убирает только нужный handler, остальные остаются."""
        bus = EventBus()
        calls = []

        def handler1(data):
            calls.append("h1")

        def handler2(data):
            calls.append("h2")

        bus.subscribe("event", handler1)
        bus.subscribe("event", handler2)
        bus.unsubscribe("event", handler1)

        bus.publish("event", data="test")

        assert calls == ["h2"]

    @patch("communication.event_bus.log")
    def test_unsubscribe_nonexistent_event(self, _mock_log):
        """Отписка от несуществующего события — не ошибка."""
        bus = EventBus()

        def handler(data):
            pass

        # Не должно выбросить исключение
        bus.unsubscribe("ghost.event", handler)

    @patch("communication.event_bus.log")
    def test_unsubscribe_nonexistent_handler(self, _mock_log):
        """Отписка handler'а который не был подписан — не ошибка."""
        bus = EventBus()

        def handler1(data):
            pass

        def handler2(data):
            pass

        bus.subscribe("event", handler1)
        # Не должно выбросить исключение
        bus.unsubscribe("event", handler2)


class TestPublishNoSubscribers:
    """Тесты публикации без подписчиков."""

    @patch("communication.event_bus.log")
    def test_publish_no_subscribers(self, _mock_log):
        """Публикация события без подписчиков — без ошибки."""
        bus = EventBus()
        # Не должно выбросить исключение
        bus.publish("no.subscribers", data="test")

    @patch("communication.event_bus.log")
    def test_publish_different_event(self, _mock_log):
        """Публикация события когда подписаны на другое — handler не вызывается."""
        bus = EventBus()
        calls = []

        def handler(data):
            calls.append(data)

        bus.subscribe("event.a", handler)
        bus.publish("event.b", data="test")

        assert calls == []


class TestHandlerError:
    """Тесты обработки ошибок в handler."""

    @patch("communication.event_bus.log")
    def test_handler_error(self, _mock_log):
        """Ошибка в handler не ломает других подписчиков."""
        bus = EventBus()
        calls = []

        def bad_handler(data):
            raise ValueError("boom")

        def good_handler(data):
            calls.append("good")

        bus.subscribe("event", bad_handler)
        bus.subscribe("event", good_handler)

        # Не должно выбросить исключение
        bus.publish("event", data="test")

        # Хороший handler всё равно вызван
        assert calls == ["good"]


class TestClear:
    """Тесты очистки подписчиков."""

    @patch("communication.event_bus.log")
    def test_clear(self, _mock_log):
        """Очистка всех подписчиков."""
        bus = EventBus()
        calls = []

        def handler1(data):
            calls.append("h1")

        def handler2(data):
            calls.append("h2")

        bus.subscribe("event1", handler1)
        bus.subscribe("event2", handler2)

        bus.clear()

        bus.publish("event1", data="test")
        bus.publish("event2", data="test")

        assert calls == []

    @patch("communication.event_bus.log")
    def test_clear_empty_bus(self, _mock_log):
        """Очистка пустой шины — не ошибка."""
        bus = EventBus()
        bus.clear()  # Не должно выбросить исключение

    @patch("communication.event_bus.log")
    def test_subscribe_after_clear(self, _mock_log):
        """Подписка после очистки работает корректно."""
        bus = EventBus()
        calls = []

        def handler(data):
            calls.append(data)

        bus.subscribe("event", handler)
        bus.clear()
        bus.subscribe("event", handler)

        bus.publish("event", data="test")

        assert calls == ["test"]
