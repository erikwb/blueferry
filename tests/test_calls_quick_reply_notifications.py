"""Ringing-call popups that decline with a message; a recording fake server."""
from __future__ import annotations

from blueferry.calls.model import CallEvent
from tests.test_calls_notifications import _record, _sink


def _reply_sink(replies=("Can't talk now. I'll call you back.",), inline=False):
    actions: list[tuple] = []
    sink = _sink(on_call_action=lambda *args: actions.append(args))
    sink._quick_replies = lambda: list(replies)
    sink._call_reply_texts = {}
    sink._server_inline_reply = inline
    return sink, actions


def test_the_first_reply_is_a_shortened_button() -> None:
    sink, actions = _reply_sink()

    sink.handle_call(CallEvent("call_incoming", _record()))

    notify = sink._notif.calls[0]
    assert list(notify[5])[:4] == ["answer", "Answer", "decline", "Decline"]
    assert list(notify[5])[4] == "decline-message"
    label = list(notify[5])[5]
    assert label.endswith("…") and len(label) <= 32
    sink._on_action(41, "decline-message")
    assert actions == [("voicecall01", "decline-message", "Can't talk now. I'll call you back.")]


def test_inline_reply_servers_get_reply_and_send_the_typed_text() -> None:
    sink, actions = _reply_sink(inline=True)

    sink.handle_call(CallEvent("call_incoming", _record()))
    notify = sink._notif.calls[0]
    sink._on_replied(41, "  See you at 5  ")
    sink._on_replied(41, "   ")
    sink._on_replied(99, "Not ours")

    assert list(notify[5])[4:] == ["inline-reply", "Reply…"]
    assert "x-kde-reply-placeholder-text" in notify[6]
    assert actions == [("voicecall01", "decline-message", "See you at 5")]


def test_no_reply_button_without_a_number_or_replies() -> None:
    withheld, _actions = _reply_sink()
    withheld.handle_call(CallEvent("call_incoming", _record(number="")))
    empty, _actions = _reply_sink(replies=())
    empty.handle_call(CallEvent("call_incoming", _record()))

    assert list(withheld._notif.calls[0][5]) == ["answer", "Answer", "decline", "Decline"]
    assert list(empty._notif.calls[0][5]) == ["answer", "Answer", "decline", "Decline"]


def test_a_reply_button_is_dead_once_the_call_is_answered() -> None:
    sink, actions = _reply_sink(inline=False)
    sink.handle_call(CallEvent("call_incoming", _record()))

    sink.handle_call(CallEvent("call_changed", _record(state="active")))
    sink._on_action(41, "decline-message")
    sink._on_replied(41, "late")

    assert actions == []
    sink.handle_call(CallEvent("call_ended", _record(state="disconnected")))
    assert sink._call_reply_texts == {}


def test_a_failed_reply_from_a_popup_is_announced_without_content() -> None:
    sink, _actions = _reply_sink()

    sink.handle_call_reply_failed()

    notify = sink._notif.calls[0]
    assert "not sent" in notify[3] + notify[4]
    assert "+4179" not in notify[4]
    quiet, _actions = _reply_sink()
    quiet._notification_policy = lambda: "none"
    quiet.handle_call_reply_failed()
    assert quiet._notif.calls == []


def test_an_overlong_inline_reply_is_refused_with_a_notice() -> None:
    sink, actions = _reply_sink(inline=True)
    sink.handle_call(CallEvent("call_incoming", _record()))

    sink._on_replied(41, "x" * 1001)

    assert actions == []
    notice = sink._notif.calls[-1]
    assert "1000" in notice[4] and "not declined" in notice[4]


def test_the_failure_notice_says_whether_the_call_was_declined() -> None:
    sink, _actions = _reply_sink()

    sink.handle_call_reply_failed(declined=False)

    assert "not declined" in sink._notif.calls[0][4]
