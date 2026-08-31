"""Event fan-out policy without desktop or device access."""

from __future__ import annotations

from types import SimpleNamespace

from blueferry import event_dispatcher
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


class _Match:
    def __init__(self) -> None:
        self.removed = False

    def remove(self) -> None:
        self.removed = True


class _Bus:
    def __init__(self, *, owner: bool = False) -> None:
        self.owner = owner
        self.callback = None
        self.match = _Match()

    def add_signal_receiver(self, callback, **kwargs):
        assert kwargs == {
            "dbus_interface": "org.freedesktop.DBus",
            "signal_name": "NameOwnerChanged",
            "bus_name": "org.freedesktop.DBus",
            "arg0": "org.freedesktop.Notifications",
        }
        self.callback = callback
        return self.match

    def name_has_owner(self, name: str) -> bool:
        assert name == "org.freedesktop.Notifications"
        return self.owner

    def change_owner(self, old_owner: str, new_owner: str) -> None:
        self.owner = bool(new_owner)
        assert self.callback is not None
        self.callback("org.freedesktop.Notifications", old_owner, new_owner)


class _SqliteSink:
    name = "sqlite"

    def __init__(self, *, storage=None) -> None:
        self.storage = storage

    def handle(self, _event) -> None:
        pass


class _NotificationSink:
    name = "libnotify"

    def __init__(self) -> None:
        self.closed = False

    def handle(self, _event) -> None:
        pass

    def close(self) -> None:
        self.closed = True


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


def test_libnotify_is_added_when_notification_server_appears(monkeypatch):
    monkeypatch.setattr(event_dispatcher, "SqliteSink", _SqliteSink)
    bus = _Bus()
    attempts = []

    def create_sink(**_kwargs):
        # Install the owner watch before the fallible construction so the
        # server cannot appear in an unobserved gap.
        assert bus.callback is not None
        assert _kwargs["contacts_only_notifications"]() is True
        attempts.append(bus.owner)
        if not bus.owner:
            raise RuntimeError("notification server is not ready")
        return _NotificationSink()

    dispatcher = EventDispatcher(
        object(),
        submit_obex=lambda *_args, **_kwargs: None,
        contacts_only_notifications=lambda: True,
        notification_sink_factory=create_sink,
        session_bus=bus,
    )

    dispatcher.setup()
    assert dispatcher.names == ["sqlite"]

    bus.change_owner("", ":1.42")

    assert attempts == [False, True]
    assert dispatcher.names == ["sqlite", "libnotify"]


def test_owned_but_not_ready_notification_server_is_retried(monkeypatch):
    monkeypatch.setattr(event_dispatcher, "SqliteSink", _SqliteSink)
    bus = _Bus(owner=True)
    scheduled = []
    attempts = []

    def create_sink(**_kwargs):
        attempts.append(True)
        if len(attempts) < 3:
            raise RuntimeError("object has not been exported yet")
        return _NotificationSink()

    dispatcher = EventDispatcher(
        object(),
        submit_obex=lambda *_args, **_kwargs: None,
        notification_sink_factory=create_sink,
        session_bus=bus,
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or len(scheduled),
    )

    dispatcher.setup()
    assert dispatcher.names == ["sqlite"]
    assert scheduled[-1][0] == event_dispatcher._LIBNOTIFY_RETRY_SECONDS

    scheduled[-1][1]()
    assert dispatcher.names == ["sqlite"]
    scheduled[-1][1]()

    assert len(attempts) == 3
    assert dispatcher.names == ["sqlite", "libnotify"]


def test_notification_owner_replacement_rebuilds_sink(monkeypatch):
    monkeypatch.setattr(event_dispatcher, "SqliteSink", _SqliteSink)
    bus = _Bus(owner=True)
    sinks = []

    def create_sink(**_kwargs):
        sink = _NotificationSink()
        sinks.append(sink)
        return sink

    dispatcher = EventDispatcher(
        object(),
        submit_obex=lambda *_args, **_kwargs: None,
        notification_sink_factory=create_sink,
        session_bus=bus,
    )
    dispatcher.setup()

    bus.change_owner(":1.42", ":1.84")

    assert len(sinks) == 2
    assert sinks[0].closed is True
    assert sinks[1].closed is False
    assert dispatcher.names == ["sqlite", "libnotify"]


def test_dispatcher_stop_removes_owner_watch_and_retry(monkeypatch):
    monkeypatch.setattr(event_dispatcher, "SqliteSink", _SqliteSink)
    bus = _Bus(owner=True)
    cancelled = []

    def unavailable_sink(**_kwargs):
        raise RuntimeError("not ready")

    dispatcher = EventDispatcher(
        object(),
        submit_obex=lambda *_args, **_kwargs: None,
        notification_sink_factory=unavailable_sink,
        session_bus=bus,
        schedule=lambda _delay, _callback: 9,
        cancel=cancelled.append,
    )
    dispatcher.setup()

    dispatcher.stop()

    assert bus.match.removed is True
    assert cancelled == [9]
