"""Call desktop popups: ringing (Answer/Decline) and ongoing (Hang up).

The notification service is a recording fake; no session bus is opened.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from blueferry.calls.model import CallEvent, CallRecord
from blueferry.event_dispatcher import EventDispatcher
from blueferry.sinks.libnotify import LibnotifySink


class _FakeNotifications:
    def __init__(self) -> None:
        self.calls = []
        self._next = 40

    def Notify(self, *args):
        self.calls.append(args)
        if int(args[1]):
            # The specification: a replacement keeps the notification's id.
            return int(args[1])
        self._next += 1
        return self._next

    def CloseNotification(self, notification_id, **kwargs):
        # Closing must never block the daemon's main loop.
        assert "reply_handler" in kwargs and "error_handler" in kwargs
        self.calls.append(("close", int(notification_id)))
        kwargs["reply_handler"]()


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

    sink.handle_call(CallEvent("call_changed", _record(state="disconnected")))
    sink.handle_call(CallEvent("call_ended", _record(state="disconnected")))

    assert [call[0] for call in sink._notif.calls] == ["BlueFerry", "close"]
    assert sink._notif.calls[1] == ("close", 41)
    _assert_no_leftovers(sink)


def test_closed_popup_forgets_its_call() -> None:
    sink = _sink(on_call_action=lambda *_args: None)
    sink.handle_call(CallEvent("call_incoming", _record()))

    sink._on_closed(41, 2)
    # Still ringing: a closed popup does not come back on its own.
    sink.handle_call(CallEvent("call_changed", _record(network_name="Alice")))
    sink.handle_call(CallEvent("call_ended", _record()))

    assert len(sink._notif.calls) == 1
    _assert_no_leftovers(sink)


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


def test_notification_hang_up_goes_through_the_controller(make_daemon) -> None:
    instance = make_daemon()
    seen = []
    instance.calls.hangup = lambda call_id, _ok, _fail: seen.append(("hangup", call_id))
    instance.calls.answer = lambda call_id, _ok, _fail: seen.append(("answer", call_id))

    instance._notification_call_action("voicecall01", "hangup")
    instance._notification_call_action("voicecall01", "bogus")

    assert seen == [("hangup", "voicecall01")]


def test_notification_hang_up_failure_is_logged_not_raised(make_daemon) -> None:
    instance = make_daemon()
    failures = []

    def hangup(_call_id, _ok, fail):
        failures.append(1)
        fail(RuntimeError("+41791234567 must not be logged"))

    instance.calls.hangup = hangup

    instance._notification_call_action("voicecall01", "hangup")

    assert failures == [1]


def test_notification_action_on_disabled_calls_is_logged_not_raised(make_daemon) -> None:
    instance = make_daemon()

    instance._notification_call_action("voicecall01", "answer")


def test_controller_events_are_routed_to_the_dispatcher(make_daemon) -> None:
    instance = make_daemon()

    assert instance.calls._on_event == instance.events.call
    assert instance.events.on_call_action == instance._notification_call_action


# ---- ongoing calls (#230) -----------------------------------------------

_START = 1793369645.0  # 2026-10-30T15:14:05+01:00


def _since(start=_START) -> str:
    return f"since {time.strftime('%H:%M', time.localtime(start))}"


def _assert_no_leftovers(sink) -> None:
    assert sink._call_notifications == {}
    assert sink._call_popups == {}
    assert getattr(sink, "_call_shown", {}) == {}
    assert getattr(sink, "_call_starts", {}) == {}


def _hints(notify) -> dict:
    return dict(notify[6])


def test_answered_call_becomes_an_ongoing_popup_until_it_ends() -> None:
    actions = []
    sink = _sink(on_call_action=lambda call_id, action: actions.append((call_id, action)))
    sink.handle_call(CallEvent("call_incoming", _record()))

    sink.handle_call(CallEvent("call_changed", _record(state="active", start_time=_START)))

    ringing, close, ongoing = sink._notif.calls
    assert close == ("close", 41)
    assert ongoing[3] == "\U0001f4de Call with &lt;b&gt;Alice&lt;/b&gt;"
    assert ongoing[4] == _since()
    assert list(ongoing[5]) == ["hangup", "Hang up"]
    hints = _hints(ongoing)
    assert int(hints["urgency"]) == 1 and bool(hints["resident"])
    assert "transient" not in hints
    assert int(ongoing[7]) == 0
    assert bool(_hints(ringing)["transient"])

    # The replaced ringing popup's close signal arrives late and must not
    # forget the ongoing popup.
    sink._on_closed(41, 3)
    sink._on_action(41, "decline")
    sink._on_action(42, "answer")
    sink._on_action(42, "hangup")
    assert actions == [("voicecall01", "hangup")]

    # Unrelated property updates (and the clock) do not re-notify.
    sink.handle_call(CallEvent("call_changed", _record(state="active", start_time=_START,
                                                       multiparty=False)))
    assert len(sink._notif.calls) == 3

    sink.handle_call(CallEvent("call_ended", _record(state="active", start_time=_START)))
    assert sink._notif.calls[-1] == ("close", 42)
    _assert_no_leftovers(sink)


def test_held_call_updates_the_same_popup_and_keeps_the_start() -> None:
    sink = _sink(on_call_action=lambda *_args: None)
    sink.handle_call(CallEvent("call_changed", _record(state="active", start_time=_START)))

    sink.handle_call(CallEvent("call_changed", _record(state="held")))
    sink.handle_call(CallEvent("call_changed", _record(state="active")))

    first, held, resumed = sink._notif.calls
    assert int(first[1]) == 0
    assert int(held[1]) == 41 and held[4] == f"On hold · {_since()}"
    assert list(held[5]) == ["hangup", "Hang up"]
    assert int(resumed[1]) == 41 and resumed[4] == _since()


def test_outgoing_call_shows_nothing_until_connected(monkeypatch) -> None:
    monkeypatch.setattr(time, "time", lambda: _START)
    sink = _sink(on_call_action=lambda *_args: None)
    outgoing = {"direction": "outgoing", "contact_name": ""}

    sink.handle_call(CallEvent("call_changed", _record(state="dialing", **outgoing)))
    sink.handle_call(CallEvent("call_changed", _record(state="alerting", **outgoing)))
    assert sink._notif.calls == []

    sink.handle_call(CallEvent("call_changed", _record(state="active", **outgoing)))
    # Without oFono's StartTime the first active sighting is the start, and
    # it stays fixed: a later update does not move it.
    monkeypatch.setattr(time, "time", lambda: _START + 600)
    sink.handle_call(CallEvent("call_changed", _record(state="held", **outgoing)))

    active, held = sink._notif.calls
    assert active[3] == "\U0001f4de Call with +41791234567"
    assert active[4] == _since()
    assert held[4] == f"On hold · {_since()}"


def test_late_ofono_start_time_corrects_an_open_popup_once(monkeypatch) -> None:
    monkeypatch.setattr(time, "time", lambda: _START + 7200)
    sink = _sink(on_call_action=lambda *_args: None)
    sink.handle_call(CallEvent("call_changed", _record(state="active")))

    sink.handle_call(CallEvent("call_changed", _record(state="active", start_time=_START)))
    sink.handle_call(CallEvent("call_changed", _record(state="active", start_time=_START)))

    first, corrected = sink._notif.calls
    assert first[4] == _since(_START + 7200)
    assert int(corrected[1]) == 41 and corrected[4] == _since()


@pytest.mark.parametrize("reason", [1, 2, 3])
def test_closing_the_ongoing_popup_never_hangs_up(reason) -> None:
    actions = []
    sink = _sink(on_call_action=lambda call_id, action: actions.append((call_id, action)))
    sink.handle_call(CallEvent("call_changed", _record(state="active", start_time=_START)))

    sink._on_closed(41, reason)
    sink._on_action(41, "hangup")
    # Same phase: it stays closed, also when the caller ID is updated.
    sink.handle_call(CallEvent("call_changed", _record(state="active", start_time=_START,
                                                       network_name="Bob")))

    assert actions == []
    assert len(sink._notif.calls) == 1

    # A real change (hold) shows it again, as a new popup.
    sink.handle_call(CallEvent("call_changed", _record(state="held", start_time=_START)))
    assert len(sink._notif.calls) == 2 and int(sink._notif.calls[1][1]) == 0

    sink.handle_call(CallEvent("call_ended", _record(state="held")))
    _assert_no_leftovers(sink)


def test_hidden_content_hides_the_caller_of_an_ongoing_call(monkeypatch) -> None:
    monkeypatch.setattr("blueferry.sinks.libnotify.config.SHOW_NOTIFICATION_CONTENT", False)
    sink = _sink(on_call_action=lambda *_args: None)

    sink.handle_call(CallEvent("call_changed", _record(state="active", start_time=_START)))
    sink.handle_call(CallEvent("call_changed", _record(state="held", start_time=_START)))

    active, held = sink._notif.calls
    assert active[3] == "\U0001f4de Call in progress" and active[4] == _since()
    assert held[4] == f"On hold · {_since()}"
    text = "".join(str(part) for part in active + held)
    assert "Alice" not in text and "4179" not in text
    assert list(active[5]) == ["hangup", "Hang up"]


def test_no_notification_policy_suppresses_ongoing_popups() -> None:
    sink = _sink(policy="none", on_call_action=lambda *_args: None)

    sink.handle_call(CallEvent("call_changed", _record(state="active", start_time=_START)))
    sink.handle_call(CallEvent("call_changed", _record(state="held", start_time=_START)))
    sink.handle_call(CallEvent("call_ended", _record(state="held")))

    assert sink._notif.calls == []
    _assert_no_leftovers(sink)


def test_policy_switched_off_mid_call_retires_the_ringing_popup() -> None:
    actions = []
    sink = _sink(on_call_action=lambda call_id, action: actions.append((call_id, action)))
    sink.handle_call(CallEvent("call_incoming", _record()))

    sink._notification_policy = lambda: "none"
    sink.handle_call(CallEvent("call_changed", _record(state="active", start_time=_START)))
    sink._on_action(41, "decline")

    assert sink._notif.calls[1:] == [("close", 41)]
    assert actions == []
    sink.handle_call(CallEvent("call_ended", _record(state="active")))
    _assert_no_leftovers(sink)


def test_ongoing_popup_without_action_callback_has_no_button() -> None:
    sink = _sink()

    sink.handle_call(CallEvent("call_changed", _record(state="active", start_time=_START)))

    assert list(sink._notif.calls[0][5]) == []


def test_waiting_call_and_held_call_each_get_one_popup() -> None:
    actions = []
    sink = _sink(on_call_action=lambda call_id, action: actions.append((call_id, action)))
    first = {"start_time": _START}
    second = {"call_id": "voicecall02", "path": "/x/voicecall02", "contact_name": "Bob",
              "number": "+41797654321"}

    sink.handle_call(CallEvent("call_changed", _record(state="active", **first)))      # 41
    sink.handle_call(CallEvent("call_incoming", _record(state="waiting", **second)))  # 42
    # Hold and answer: the first call is held, the second becomes active.
    sink.handle_call(CallEvent("call_changed", _record(state="held", **first)))       # 41 again
    sink.handle_call(CallEvent("call_changed", _record(
        state="active", start_time=_START + 60, **second,
    )))                                                                                # close 42, 43
    assert sink._call_popups == {"voicecall01": 41, "voicecall02": 43}
    held = sink._notif.calls[2]
    assert int(held[1]) == 41 and "Alice" in held[3] and held[4].startswith("On hold")
    assert sink._notif.calls[3] == ("close", 42)
    assert "Bob" in sink._notif.calls[4][3]

    sink._on_action(41, "hangup")
    sink._on_action(43, "hangup")
    assert actions == [("voicecall01", "hangup"), ("voicecall02", "hangup")]

    # Swap, then both end.
    sink.handle_call(CallEvent("call_changed", _record(state="active", **first)))
    sink.handle_call(CallEvent("call_changed", _record(
        state="held", start_time=_START + 60, **second,
    )))
    sink.handle_call(CallEvent("call_ended", _record(state="active", **first)))
    sink.handle_call(CallEvent("call_ended", _record(state="held", **second)))
    closes = [call for call in sink._notif.calls if call[0] == "close"]
    assert closes[-2:] == [("close", 41), ("close", 43)]
    _assert_no_leftovers(sink)


def test_call_tables_stay_bounded_without_end_events() -> None:
    from blueferry.calls.model import MAX_TRACKED_CALLS

    sink = _sink(policy="none")
    for index in range(MAX_TRACKED_CALLS * 3):
        sink.handle_call(CallEvent("call_changed", _record(
            state="active", call_id=f"voicecall{index}", path=f"/x/voicecall{index}",
        )))

    assert len(sink._call_starts) == MAX_TRACKED_CALLS


def test_shutdown_retires_call_popups() -> None:
    sink = _sink(on_call_action=lambda *_args: None)
    sink.handle_call(CallEvent("call_incoming", _record()))
    sink.handle_call(CallEvent("call_changed", _record(
        state="active", call_id="voicecall02", path="/x/voicecall02",
    )))

    dispatcher = EventDispatcher(object(), defer_mark_read=lambda _path: None)
    dispatcher.sinks = [sink]
    dispatcher._remove_libnotify_sink = lambda **_kwargs: None
    dispatcher.stop()

    assert sorted(call for call in sink._notif.calls if call[0] == "close") == [
        ("close", 41), ("close", 42),
    ]
    _assert_no_leftovers(sink)


def test_server_that_replaces_with_a_new_id_leaves_no_stale_mapping() -> None:
    sink = _sink(on_call_action=lambda *_args: None)
    sink._notif.Notify = lambda *args: sink._notif.calls.append(args) or 50 + len(sink._notif.calls)
    sink.handle_call(CallEvent("call_changed", _record(state="active", start_time=_START)))
    sink.handle_call(CallEvent("call_changed", _record(state="held", start_time=_START)))

    assert sink._call_notifications == {52: "voicecall01"}
    sink._on_closed(51, 3)
    assert sink._call_popups == {"voicecall01": 52}


def test_policy_none_retires_open_call_popups_at_once() -> None:
    policy = ["messages"]
    sink = _sink(on_call_action=lambda *_args: None)
    sink._notification_policy = lambda: policy[0]
    sink.handle_call(CallEvent("call_incoming", _record()))
    sink.handle_call(CallEvent("call_changed", _record(
        state="active", start_time=_START, call_id="voicecall02", path="/x/voicecall02",
    )))
    dispatcher = EventDispatcher(object(), defer_mark_read=lambda _path: None)
    dispatcher.sinks = [sink]

    dispatcher.notification_policy_changed()  # still "messages": nothing closes
    assert not [call for call in sink._notif.calls if call[0] == "close"]

    policy[0] = "none"
    dispatcher.notification_policy_changed()
    assert sorted(call for call in sink._notif.calls if call[0] == "close") == [
        ("close", 41), ("close", 42),
    ]
    assert sink._call_popups == {} and sink._call_notifications == {}
    assert sink._call_starts == {"voicecall02": _START}

    # Relaxed again: the next event shows the call with its original start.
    policy[0] = "messages"
    sink.handle_call(CallEvent("call_changed", _record(
        state="held", call_id="voicecall02", path="/x/voicecall02",
    )))
    assert sink._notif.calls[-1][4] == f"On hold · {_since()}"


def test_daemon_policy_change_reaches_the_call_popups(make_daemon) -> None:
    instance = make_daemon()
    seen = []
    instance.events.notification_policy_changed = lambda: seen.append(1)

    instance._notification_policy_changed()

    assert seen == [1]


def test_trimmed_call_entry_takes_its_popup_along() -> None:
    from blueferry.calls.model import MAX_TRACKED_CALLS

    sink = _sink(on_call_action=lambda *_args: None)
    for index in range(MAX_TRACKED_CALLS + 1):
        sink.handle_call(CallEvent("call_changed", _record(
            state="active", start_time=_START,
            call_id=f"voicecall{index}", path=f"/x/voicecall{index}",
        )))

    assert ("close", 41) in sink._notif.calls
    assert "voicecall0" not in sink._call_popups and 41 not in sink._call_notifications
    assert len(sink._call_shown) == len(sink._call_popups) == MAX_TRACKED_CALLS
