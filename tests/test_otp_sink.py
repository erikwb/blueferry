"""The opt-in one-time code sink: only new incoming messages, never leaks."""
from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from blueferry import event_dispatcher
from blueferry.event_dispatcher import EventDispatcher
from blueferry.events import SmsEvent, sms_sent_event
from blueferry.sinks import otp_clipboard as sink_module
from blueferry.sinks.otp_clipboard import DesktopNotifier, OtpClipboardSink, notification_text

CODE = "482913"
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
BODY = f"Your verification code is {CODE}. Don't share it."


class _Writer:
    """Fake ClipboardWriter; ``outcomes`` scripts each copy's final state."""

    def __init__(self, *, outcomes=None, clear_after_s=0) -> None:
        self.copied: list[tuple[str, frozenset]] = []
        self.outcomes = list(outcomes or [])
        self.clear_after_s = clear_after_s
        self.released = False
        self.probed = False
        self.owner = None

    def start_probe(self) -> None:
        self.probed = True

    def copy(self, code: str, *, exclude=frozenset()):
        self.copied.append((code, frozenset(exclude)))
        state = self.outcomes.pop(0) if self.outcomes else "running"
        if state is None:
            return None
        tool = "xclip" if "wl-copy" in exclude else "wl-copy"
        ticket = SimpleNamespace(tool=tool, final_state=state)
        self.owner = ticket
        return ticket

    def state(self, ticket):
        if ticket is not self.owner:
            return "superseded"
        return ticket.final_state

    def close(self) -> None:
        self.released = True


class _Notifier:
    def __init__(self) -> None:
        self.shown: list[tuple[str, str]] = []
        self.closed = False

    def notify(self, summary: str, body: str) -> None:
        self.shown.append((summary, body))

    def close(self) -> None:
        self.closed = True


class _Timers:
    def __init__(self) -> None:
        self.pending: dict[int, object] = {}
        self.next_id = 1

    def schedule(self, _delay, callback) -> int:
        source = self.next_id
        self.next_id += 1
        self.pending[source] = callback
        return source

    def cancel(self, source) -> None:
        self.pending.pop(source, None)

    def settle(self) -> None:
        while self.pending:
            source = next(iter(self.pending))
            self.pending.pop(source)()


def _received(body=BODY, *, handle="message1", path="/org/bluez/obex/client/session1/message1",
              age=timedelta(seconds=5), kind="sms_received") -> SmsEvent:
    return SmsEvent(
        kind=kind,
        handle=handle,
        sender_address="+15551234567",
        sender_phone_norm="15551234567",
        contact_name=None,
        body=body,
        timestamp=NOW - age,
        is_read=False,
        message_path=path,
    )


def _sink(writer=None, *, policy="messages"):
    notifier = _Notifier()
    timers = _Timers()
    sink = OtpClipboardSink(
        writer=writer or _Writer(),
        notification_policy=lambda: policy,
        notifier=notifier,
        schedule_ms=timers.schedule,
        cancel=timers.cancel,
        now=lambda: NOW,
    )
    return sink, notifier, timers


def _codes(writer) -> list[str]:
    return [code for code, _exclude in writer.copied]


def test_new_incoming_code_is_copied_and_announced(monkeypatch) -> None:
    monkeypatch.setattr(sink_module.config, "SHOW_NOTIFICATION_CONTENT", False)
    writer = _Writer()
    sink, notifier, timers = _sink(writer)

    assert writer.probed is True
    sink.handle(_received())
    timers.settle()

    assert _codes(writer) == [CODE]
    assert notifier.shown == [("Verification code copied", "Paste it with Ctrl+V.")]


@pytest.mark.parametrize(
    "event",
    [
        sms_sent_event("+15551234567", BODY),
        _received(kind="sms_seen"),
        # Listed/reconstructed records carry no live Message1 path.
        _received(path=None),
        # A stale message replayed after a reconnect.
        _received(age=timedelta(minutes=30)),
        _received(body="See you at 18:30"),
        # Fail closed on missing, zone-less, or implausible times.
        replace(_received(), timestamp=None),
        replace(_received(), timestamp=NOW.replace(tzinfo=None)),
        _received(age=-timedelta(minutes=5)),
        # Already read on the phone.
        replace(_received(), is_read=True),
        # Saved contacts and group conversations are chat, not services.
        replace(_received(), contact_name="Anna"),
        replace(_received(), group_key="group:abc", group_name="Family"),
    ],
)
def test_only_new_incoming_messages_with_a_code_are_copied(event) -> None:
    writer = _Writer()
    sink, notifier, timers = _sink(writer)

    sink.handle(event)
    timers.settle()

    assert writer.copied == []
    assert notifier.shown == []


def test_stale_message_is_logged_without_content(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    sink, _notifier, _timers = _sink()

    sink.handle(_received(age=timedelta(minutes=30)))

    assert "ignoring a message 1800 seconds old" in caplog.text
    assert CODE not in caplog.text


def test_small_clock_skew_into_the_future_is_accepted() -> None:
    writer = _Writer()
    sink, _notifier, timers = _sink(writer)

    sink.handle(_received(age=-timedelta(seconds=60)))
    timers.settle()

    assert _codes(writer) == [CODE]


def test_messages_from_before_the_backend_started_are_not_replayed() -> None:
    writer = _Writer()
    notifier = _Notifier()
    timers = _Timers()
    clock = [NOW]
    sink = OtpClipboardSink(
        writer=writer,
        notification_policy=lambda: "messages",
        notifier=notifier,
        schedule_ms=timers.schedule,
        cancel=timers.cancel,
        now=lambda: clock[0],
    )

    # Younger than ten minutes, but stamped well before the sink started.
    sink.handle(_received(handle="old", age=timedelta(minutes=2)))
    sink.handle(_received(handle="new", age=timedelta(seconds=10)))
    timers.settle()

    assert _codes(writer) == [CODE]


def test_bursts_of_codes_are_rate_limited(caplog) -> None:
    writer = _Writer()
    notifier = _Notifier()
    timers = _Timers()
    clock = [NOW]
    sink = OtpClipboardSink(
        writer=writer,
        notification_policy=lambda: "messages",
        notifier=notifier,
        schedule_ms=timers.schedule,
        cancel=timers.cancel,
        now=lambda: clock[0],
    )

    for index in range(5):
        sink.handle(_received(handle=f"m{index}", age=timedelta(0)))
    assert len(writer.copied) == sink_module.MAX_COPIES_PER_WINDOW
    assert "too many codes" in caplog.text

    clock[0] = NOW + sink_module.COPY_WINDOW + timedelta(seconds=1)
    sink.handle(_received(handle="later", age=timedelta(0)))
    assert len(writer.copied) == sink_module.MAX_COPIES_PER_WINDOW + 1


def test_the_same_message_is_copied_only_once() -> None:
    writer = _Writer()
    sink, _notifier, timers = _sink(writer)

    sink.handle(_received())
    sink.handle(_received())
    timers.settle()

    assert _codes(writer) == [CODE]


def test_failed_wl_copy_falls_back_to_x11_once(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    writer = _Writer(outcomes=["failed", "running"])
    sink, notifier, timers = _sink(writer)

    sink.handle(_received())
    timers.settle()

    assert writer.copied == [(CODE, frozenset()), (CODE, frozenset({"wl-copy"}))]
    assert len(notifier.shown) == 1
    assert "copied a one-time code to the clipboard via xclip" in caplog.text
    assert CODE not in caplog.text


def test_fallback_warning_is_logged_once(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    writer = _Writer(outcomes=["failed", "running", "failed", "running"])
    sink, notifier, timers = _sink(writer)

    sink.handle(_received(handle="message1"))
    timers.settle()
    sink.handle(_received(handle="message2"))
    timers.settle()

    assert len(writer.copied) == 4
    assert len(notifier.shown) == 2
    assert caplog.text.count("trying X11 helpers") == 1


def test_failed_fallback_does_not_claim_success(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    writer = _Writer(outcomes=["failed", "failed"])
    sink, notifier, timers = _sink(writer)

    sink.handle(_received())
    timers.settle()

    assert len(writer.copied) == 2
    assert notifier.shown == []
    assert "xclip could not take the clipboard" in caplog.text
    assert CODE not in caplog.text


def test_two_codes_within_the_confirm_delay_confirm_only_the_newer() -> None:
    writer = _Writer(outcomes=["running", "running"])
    sink, notifier, timers = _sink(writer)

    sink.handle(_received(handle="message1"))
    sink.handle(_received(body="Your verification code is 135790", handle="message2"))
    timers.settle()

    assert _codes(writer) == [CODE, "135790"]
    assert len(notifier.shown) == 1


def test_close_cancels_pending_confirmations() -> None:
    writer = _Writer()
    sink, notifier, timers = _sink(writer)
    sink.handle(_received())

    sink.close()
    timers.settle()

    assert notifier.shown == []
    assert notifier.closed is True
    assert writer.released is True


def test_notifications_off_still_copies_silently() -> None:
    writer = _Writer()
    sink, notifier, timers = _sink(writer, policy="none")

    sink.handle(_received())
    timers.settle()

    assert _codes(writer) == [CODE]
    assert notifier.shown == []


def test_code_is_shown_only_when_content_is_allowed() -> None:
    shown = notification_text(CODE, "Alice", show_content=True, clear_after_s=30)
    hidden = notification_text(CODE, "Alice", show_content=False, clear_after_s=30)

    assert CODE in shown[0]
    assert "Alice" in shown[1]
    assert "30 seconds" in shown[1]
    assert all(CODE not in part and "Alice" not in part for part in hidden)


def test_notification_text_escapes_remote_sender() -> None:
    _summary, body = notification_text(CODE, "<b>Bank</b>", show_content=True, clear_after_s=0)
    assert "<b>" not in body


def test_code_never_reaches_the_log(caplog, monkeypatch) -> None:
    monkeypatch.setattr(sink_module.config, "SHOW_NOTIFICATION_CONTENT", True)
    caplog.set_level(logging.DEBUG)
    sink, notifier, timers = _sink()

    sink.handle(_received())
    timers.settle()

    assert notifier.shown and CODE in notifier.shown[0][0]
    assert "copied a one-time code" in caplog.text
    assert CODE not in caplog.text
    assert "15551234567" not in caplog.text


# ---- desktop notifier ---------------------------------------------------


class _NotificationBus:
    def __init__(self, *, owner: bool) -> None:
        self.owner = owner
        self.callback = None
        self.lookups = 0
        self.calls = []
        self.match = SimpleNamespace(removed=False)
        self.match.remove = lambda: setattr(self.match, "removed", True)

    def add_signal_receiver(self, callback, **kwargs):
        assert kwargs["arg0"] == "org.freedesktop.Notifications"
        self.callback = callback
        return self.match

    def call_async(self, bus_name, path, interface, method, signature, args,
                   reply_handler, error_handler):
        assert (bus_name, method, signature) == ("org.freedesktop.DBus", "NameHasOwner", "s")
        assert args == ("org.freedesktop.Notifications",)
        self.pending_reply = (reply_handler, error_handler)

    def reply(self) -> None:
        reply_handler, _error_handler = self.pending_reply
        reply_handler(self.owner)

    def name_has_owner(self, _name):
        raise AssertionError("the owner query must be asynchronous")

    def get_object(self, _name, _path):
        self.lookups += 1
        bus = self

        class _Object:
            def get_dbus_method(self, member, dbus_interface=None):
                def call(*args, **kwargs):
                    bus.calls.append((member, args, kwargs))
                return call

        return _Object()


def test_notifier_follows_the_notification_owner() -> None:
    bus = _NotificationBus(owner=False)
    notifier = DesktopNotifier(bus)
    bus.reply()

    notifier.notify("Verification code copied", "Paste it with Ctrl+V.")
    assert bus.lookups == 0

    bus.callback("org.freedesktop.Notifications", "", ":1.7")
    notifier.notify("Verification code copied", "Paste it with Ctrl+V.")
    notifier.notify("Verification code copied", "Paste it with Ctrl+V.")
    assert bus.lookups == 1
    [(_member, args, kwargs)] = bus.calls[:1]
    hints = args[6]
    assert bool(hints["transient"]) is True
    assert "category" not in hints
    assert "reply_handler" in kwargs and "error_handler" in kwargs

    # A replaced notification daemon is resolved again.
    bus.callback("org.freedesktop.Notifications", ":1.7", ":1.9")
    notifier.notify("Verification code copied", "Paste it with Ctrl+V.")
    assert bus.lookups == 2

    notifier.close()
    assert bus.match.removed is True


def test_notifier_without_a_bus_is_inert() -> None:
    class _BrokenBus:
        def add_signal_receiver(self, *_args, **_kwargs):
            raise RuntimeError("no bus")

    notifier = DesktopNotifier(_BrokenBus())
    notifier.notify("Verification code copied", "Paste it with Ctrl+V.")
    notifier.close()


# ---- dispatcher wiring --------------------------------------------------


class _SqliteSink:
    name = "sqlite"

    def __init__(self, *, storage=None) -> None:
        pass

    def handle(self, _event) -> None:
        pass


class _Bus:
    def add_signal_receiver(self, *_args, **_kwargs):
        return None

    def name_has_owner(self, _name) -> bool:
        return False


def _failing_notification_sink(**_kwargs):
    raise RuntimeError("no notification server")


def _dispatcher(monkeypatch, *, enabled: bool, factory):
    monkeypatch.setattr(event_dispatcher, "SqliteSink", _SqliteSink)
    return EventDispatcher(
        object(),
        defer_mark_read=lambda _path: None,
        notification_sink_factory=_failing_notification_sink,
        session_bus=_Bus(),
        otp_autocopy=lambda: enabled,
        otp_sink_factory=factory,
        schedule=lambda *_args: 1,
        cancel=lambda _source: None,
    )


def test_disabled_autocopy_never_builds_the_sink(monkeypatch) -> None:
    built = []
    dispatcher = _dispatcher(
        monkeypatch, enabled=False, factory=lambda **kwargs: built.append(kwargs)
    )

    dispatcher.setup()
    dispatcher.message(_received())

    assert built == []
    assert "otp-clipboard" not in dispatcher.names


def test_autocopy_is_off_by_default_and_configurable(tmp_path) -> None:
    import os
    import subprocess
    import sys

    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith("BLUEFERRY_")
    }
    env.update(HOME=str(tmp_path), XDG_CONFIG_HOME=str(tmp_path / "config"),
               XDG_STATE_HOME=str(tmp_path / "state"))
    script = (
        "from blueferry import config; "
        "print(config.OTP_AUTOCOPY, config.OTP_CLEAR_SECONDS, "
        "'BLUEFERRY_OTP_AUTOCOPY' in config.LOCAL_ENV_KEYS, "
        "'BLUEFERRY_OTP_CLEAR_SECONDS' in config.LOCAL_ENV_KEYS)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True,
        check=True, timeout=30,
    )
    assert result.stdout.split() == ["False", "0", "True", "True"]


def test_enabled_autocopy_receives_messages_and_is_released_on_stop(monkeypatch) -> None:
    writer = _Writer()
    sink, _notifier, timers = _sink(writer)
    received_kwargs = []

    def factory(**kwargs):
        received_kwargs.append(kwargs)
        return sink

    dispatcher = _dispatcher(monkeypatch, enabled=True, factory=factory)

    dispatcher.setup()
    dispatcher.setup()
    assert dispatcher.names.count("otp-clipboard") == 1
    assert isinstance(received_kwargs[0]["session_bus"], _Bus)

    dispatcher.message(_received())
    timers.settle()
    assert _codes(writer) == [CODE]

    dispatcher.stop()
    assert writer.released is True
    assert "otp-clipboard" not in dispatcher.names


def test_default_config_leaves_autocopy_disabled(make_daemon, monkeypatch) -> None:
    monkeypatch.delenv("BLUEFERRY_OTP_AUTOCOPY", raising=False)
    daemon = make_daemon()

    monkeypatch.setattr(event_dispatcher, "SqliteSink", _SqliteSink)
    monkeypatch.setattr(event_dispatcher.config, "OTP_AUTOCOPY", False)
    daemon.events._session_bus = _Bus()
    daemon.events._notification_sink_factory = _failing_notification_sink
    daemon.events._schedule = lambda *_args: 1
    daemon.events.setup()

    assert "otp-clipboard" not in daemon.events.names


def test_notifier_learns_an_existing_server_asynchronously() -> None:
    bus = _NotificationBus(owner=True)
    notifier = DesktopNotifier(bus)

    notifier.notify("Verification code copied", "Paste it with Ctrl+V.")
    assert bus.lookups == 0  # reply not in yet: no popup, no blocking call

    bus.reply()
    notifier.notify("Verification code copied", "Paste it with Ctrl+V.")
    assert bus.lookups == 1


def test_a_late_owner_reply_does_not_override_a_newer_owner_change() -> None:
    bus = _NotificationBus(owner=True)
    notifier = DesktopNotifier(bus)
    bus.callback("org.freedesktop.Notifications", ":1.7", "")
    bus.reply()  # stale "has owner" answer

    notifier.notify("Verification code copied", "Paste it with Ctrl+V.")
    assert bus.lookups == 0
