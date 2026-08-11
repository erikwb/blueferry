"""Event fan-out policy without desktop or device access."""

from __future__ import annotations

from types import SimpleNamespace

from blueferry.event_dispatcher import EventDispatcher
from blueferry.events import sms_sent_event


class _Sink:
    name = "fake"

    def __init__(self) -> None:
        self.events = []

    def handle_ancs(self, event) -> None:
        self.events.append(event)


class _Service:
    def __init__(self) -> None:
        self.events = []

    def emit_history_changed(self) -> None:
        self.events.append("changed")

    def emit_open_message(self, handle: str) -> None:
        self.events.append(("open", handle))


def _event(body: str = "hello"):
    return SimpleNamespace(
        notification_id=42,
        device_path="/org/bluez/hci0/dev_TEST",
        app_id="com.apple.MobileSMS",
        app_name="Messages",
        title="Alice",
        subtitle="",
        body=body,
        category="Social",
        is_silent=False,
        is_preexisting=False,
        positive_action=None,
        negative_action=None,
    )


def _historical(event) -> dict:
    return {
        "kind": "ancs_notification",
        **{
            field: getattr(event, field)
            for field in (
                "notification_id",
                "device_path",
                "app_id",
                "app_name",
                "title",
                "subtitle",
                "body",
                "category",
                "is_silent",
                "is_preexisting",
                "positive_action",
                "negative_action",
            )
        },
        "seen_at": "2026-08-08T12:00:00+00:00",
    }


def test_replayed_ancs_event_is_not_sent_to_any_sink():
    event = _event()
    dispatcher = EventDispatcher(
        object(),
        submit_obex=lambda *_args, **_kwargs: None,
        historical_ancs=[_historical(event)],
    )
    sink = _Sink()
    service = _Service()
    dispatcher.sinks = [sink]
    dispatcher.set_dbus_service(service)

    dispatcher.ancs(event)

    assert sink.events == []
    assert service.events == []


def test_modified_notification_with_same_id_is_delivered_once():
    previous = _event("old body")
    current = _event("new body")
    dispatcher = EventDispatcher(
        object(),
        submit_obex=lambda *_args, **_kwargs: None,
        historical_ancs=[_historical(previous)],
    )
    sink = _Sink()
    service = _Service()
    dispatcher.sinks = [sink]
    dispatcher.set_dbus_service(service)

    dispatcher.ancs(current)
    dispatcher.ancs(current)

    assert sink.events == [current]
    assert service.events == ["changed"]


def test_system_notification_only_reaches_explicit_ephemeral_sink():
    event = _event()
    event.app_id = "com.example.App"
    durable = _Sink()
    ephemeral = _Sink()
    ephemeral.accepts_system_ancs = True
    service = _Service()
    dispatcher = EventDispatcher(
        object(),
        submit_obex=lambda *_args, **_kwargs: None,
    )
    dispatcher.sinks = [durable, ephemeral]
    dispatcher.set_dbus_service(service)

    dispatcher.ancs(event)

    assert durable.events == []
    assert ephemeral.events == [event]
    assert service.events == []


def test_only_an_incoming_map_message_verifies_message_notifications():
    verified = []
    dispatcher = EventDispatcher(
        object(),
        submit_obex=lambda *_args, **_kwargs: None,
        on_incoming_message=lambda: verified.append(True),
    )

    dispatcher.message(sms_sent_event("+15551234567", "sent"))
    dispatcher.message(SimpleNamespace(kind="sms_received", handle="received"))

    assert verified == [True]


def test_notification_action_is_broadcast_through_current_dbus_service():
    dispatcher = EventDispatcher(
        object(),
        submit_obex=lambda *_args, **_kwargs: None,
    )
    service = _Service()
    dispatcher.set_dbus_service(service)

    dispatcher._open_message("message-opaque-42")

    assert service.events == [("open", "message-opaque-42")]
