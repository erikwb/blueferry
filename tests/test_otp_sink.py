"""The opt-in one-time code sink: only new incoming messages, never leaks."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from blueferry import event_dispatcher
from blueferry.event_dispatcher import EventDispatcher
from blueferry.events import SmsEvent, sms_sent_event
from blueferry.sinks import otp_clipboard as sink_module
from blueferry.sinks.otp_clipboard import OtpClipboardSink, notification_text

CODE = "482913"
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
BODY = f"Your verification code is {CODE}. Don't share it."


class _Writer:
    def __init__(self, *, tool="wl-copy", failed=False, clear_after_s=0) -> None:
        self.copied: list[str] = []
        self.tool = tool
        self.failed = failed
        self.clear_after_s = clear_after_s
        self.released = False

    def copy(self, code: str):
        self.copied.append(code)
        return self.tool

    def helper_failed(self) -> bool:
        return self.failed

    def release(self) -> None:
        self.released = True


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
    notified: list[tuple[str, str]] = []
    scheduled = []
    sink = OtpClipboardSink(
        writer=writer or _Writer(),
        notification_policy=lambda: policy,
        notify=lambda summary, body: notified.append((summary, body)),
        schedule_ms=lambda delay, callback: scheduled.append(callback) or 1,
        now=lambda: NOW,
    )

    def settle() -> None:
        while scheduled:
            scheduled.pop(0)()

    return sink, notified, settle


def test_new_incoming_code_is_copied_and_announced(monkeypatch) -> None:
    monkeypatch.setattr(sink_module.config, "SHOW_NOTIFICATION_CONTENT", False)
    writer = _Writer()
    sink, notified, settle = _sink(writer)

    sink.handle(_received())
    settle()

    assert writer.copied == [CODE]
    assert notified == [("Verification code copied", "Paste it with Ctrl+V.")]


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
    ],
)
def test_only_new_incoming_messages_with_a_code_are_copied(event) -> None:
    writer = _Writer()
    sink, notified, settle = _sink(writer)

    sink.handle(event)
    settle()

    assert writer.copied == []
    assert notified == []


def test_the_same_message_is_copied_only_once() -> None:
    writer = _Writer()
    sink, _notified, settle = _sink(writer)

    sink.handle(_received())
    sink.handle(_received())
    settle()

    assert writer.copied == [CODE]


def test_failed_helper_does_not_claim_success(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    sink, notified, settle = _sink(_Writer(failed=True))

    sink.handle(_received())
    settle()

    assert notified == []
    assert CODE not in caplog.text


def test_notifications_off_still_copies_silently() -> None:
    writer = _Writer()
    sink, notified, settle = _sink(writer, policy="none")

    sink.handle(_received())
    settle()

    assert writer.copied == [CODE]
    assert notified == []


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
    sink, notified, settle = _sink()

    sink.handle(_received())
    settle()

    assert notified and CODE in notified[0][0]
    assert "copied a one-time code" in caplog.text
    assert CODE not in caplog.text
    assert "15551234567" not in caplog.text


def test_close_releases_the_clipboard_helper() -> None:
    writer = _Writer()
    sink, _notified, _settle = _sink(writer)

    sink.close()

    assert writer.released is True


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
    sink, _notified, settle = _sink(writer)
    dispatcher = _dispatcher(monkeypatch, enabled=True, factory=lambda **_kwargs: sink)

    dispatcher.setup()
    dispatcher.setup()
    assert dispatcher.names.count("otp-clipboard") == 1

    dispatcher.message(_received())
    settle()
    assert writer.copied == [CODE]

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
