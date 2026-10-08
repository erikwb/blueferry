"""Incoming-call desktop popups and their Answer/Decline actions.

The notification service is a recording fake; no session bus is opened.
"""
from __future__ import annotations

from types import SimpleNamespace

from blueferry.calls.model import CallEvent, CallRecord
from blueferry.event_dispatcher import EventDispatcher
from blueferry.sinks.libnotify import LibnotifySink


class _FakeNotifications:
    def __init__(self) -> None:
        self.calls = []
        self._next = 40

    def Notify(self, *args):
        self.calls.append(args)
        self._next += 1
        return self._next

    def CloseNotification(self, notification_id):
        self.calls.append(("close", int(notification_id)))


def _sink(policy="messages", on_call_action=None):
    sink = LibnotifySink.__new__(LibnotifySink)
    sink._notification_policy = lambda: policy
    sink._notif = _FakeNotifications()
    sink._pending = {}
    sink._msg_subs = {}
    sink._on_call_action = on_call_action
    return sink


def _record(state="incoming", **overrides):
    values = {
        "call_id": "voicecall01",
        "path": "/hfp/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF/voicecall01",
        "state": state,
        "direction": "incoming",
        "number": "+41791234567",
        "contact_name": "<b>Alice</b>",
    }
    values.update(overrides)
    return CallRecord(**values)


def test_ringing_call_popup_offers_answer_and_decline() -> None:
    actions = []
    sink = _sink(on_call_action=lambda call_id, action: actions.append((call_id, action)))

    sink.handle_call(CallEvent("call_incoming", _record()))

    notify = sink._notif.calls[0]
    assert "Incoming call" in notify[3]
    assert "&lt;b&gt;Alice&lt;/b&gt;" in notify[4] and "+41791234567" in notify[4]
    assert list(notify[5]) == ["answer", "Answer", "decline", "Decline"]
    assert int(notify[6]["urgency"]) == 2
    assert int(notify[7]) == 0

    sink._on_action(41, "answer")
    sink._on_action(41, "decline")
    sink._on_action(41, "default")
    sink._on_action(99, "answer")
    assert actions == [("voicecall01", "answer"), ("voicecall01", "decline")]


def test_popup_closes_when_the_call_stops_ringing_or_ends() -> None:
    sink = _sink(on_call_action=lambda *_args: None)
    sink.handle_call(CallEvent("call_incoming", _record()))
    # Duplicate ringing updates do not stack popups.
    sink.handle_call(CallEvent("call_changed", _record(number="+41791234567")))

    sink.handle_call(CallEvent("call_changed", _record(state="active")))
    sink.handle_call(CallEvent("call_ended", _record(state="active")))

    assert [call[0] for call in sink._notif.calls] == ["BlueFerry", "close"]
    assert sink._notif.calls[1] == ("close", 41)


def test_closed_popup_forgets_its_call() -> None:
    sink = _sink(on_call_action=lambda *_args: None)
    sink.handle_call(CallEvent("call_incoming", _record()))

    sink._on_closed(41, 2)
    sink.handle_call(CallEvent("call_ended", _record()))

    assert len(sink._notif.calls) == 1


def test_hidden_notification_content_hides_the_caller(monkeypatch) -> None:
    monkeypatch.setattr("blueferry.sinks.libnotify.config.SHOW_NOTIFICATION_CONTENT", False)
    sink = _sink(on_call_action=lambda *_args: None)

    sink.handle_call(CallEvent("call_incoming", _record()))
    sink.handle_call(CallEvent("call_incoming", _record(
        state="waiting", call_id="voicecall02", path="/x/voicecall02",
    )))

    first, second = sink._notif.calls
    assert first[4] == "Incoming call" and second[4] == "Call waiting"
    assert "Alice" not in first[4] + second[4] and "+4179" not in first[4] + second[4]
    # The actions still work without revealing who is calling.
    assert list(first[5]) == ["answer", "Answer", "decline", "Decline"]


def test_contacts_only_does_not_suppress_unknown_callers() -> None:
    sink = _sink(on_call_action=lambda *_args: None)
    sink._contacts_only_notifications = lambda: True

    sink.handle_call(CallEvent("call_incoming", _record(contact_name="")))

    assert len(sink._notif.calls) == 1


def test_waiting_call_and_missing_actions() -> None:
    sink = _sink()

    sink.handle_call(CallEvent("call_incoming", _record(state="waiting", contact_name="")))

    notify = sink._notif.calls[0]
    assert "Call waiting" in notify[3]
    assert notify[4] == "+41791234567"
    assert list(notify[5]) == []


def test_no_notification_policy_suppresses_call_popups() -> None:
    sink = _sink(policy="none", on_call_action=lambda *_args: None)

    sink.handle_call(CallEvent("call_incoming", _record()))

    assert sink._notif.calls == []


def test_dispatcher_routes_calls_only_to_call_aware_sinks() -> None:
    received = []

    class CallSink:
        name = "libnotify"

        def handle_call(self, event):
            received.append(event.kind)

    class MessageOnly:
        name = "sqlite"

        def handle(self, _event):
            raise AssertionError("calls are not message history")

    class Broken:
        name = "broken"

        def handle_call(self, _event):
            raise RuntimeError("sink failure must not stop fan-out")

    dispatcher = EventDispatcher(object(), defer_mark_read=lambda _path: None)
    dispatcher.sinks = [MessageOnly(), Broken(), CallSink()]
    history = []
    dispatcher.dbus_service = SimpleNamespace(emit_history_changed=lambda: history.append(1))

    dispatcher.call(CallEvent("call_incoming", _record()))

    assert received == ["call_incoming"]
    assert history == []


def test_dispatcher_passes_the_call_action_to_the_notification_sink() -> None:
    created = {}

    def factory(**kwargs):
        created.update(kwargs)
        return SimpleNamespace(name="libnotify")

    bus = SimpleNamespace(add_signal_receiver=lambda *_a, **_k: SimpleNamespace(remove=lambda: None))
    action = object()
    dispatcher = EventDispatcher(
        object(),
        defer_mark_read=lambda _path: None,
        on_call_action=action,
        notification_sink_factory=factory,
        session_bus=bus,
        storage=None,
    )
    dispatcher._setup_complete = True
    dispatcher._ensure_libnotify_sink()

    assert created["on_call_action"] is action


def test_notification_action_answers_or_declines_through_the_controller(make_daemon) -> None:
    instance = make_daemon()
    seen = []
    instance.calls.answer = lambda call_id, _ok, _fail: seen.append(("answer", call_id))
    instance.calls.hangup = lambda call_id, _ok, _fail: seen.append(("hangup", call_id))

    instance._notification_call_action("voicecall01", "answer")
    instance._notification_call_action("voicecall01", "decline")

    assert seen == [("answer", "voicecall01"), ("hangup", "voicecall01")]


def test_notification_action_on_disabled_calls_is_logged_not_raised(make_daemon) -> None:
    instance = make_daemon()

    instance._notification_call_action("voicecall01", "answer")


def test_controller_events_are_routed_to_the_dispatcher(make_daemon) -> None:
    instance = make_daemon()

    assert instance.calls._on_event == instance.events.call
    assert instance.events.on_call_action == instance._notification_call_action
