"""The quick reply box's decisions, without a toolkit."""
from __future__ import annotations

from blueferry.conversation_state import ReplyDisposition
from blueferry.models import Thread, ThreadMessage
from blueferry.quick_reply_state import (
    MAX_BUBBLES,
    QUICK_REPLY,
    CloseReason,
    LoadResult,
    QuickReplyState,
    SendAction,
    SendState,
    click_opens_quick_reply,
)


def _message(handle: str, body: str = "hello", *, outgoing: bool = False) -> ThreadMessage:
    return ThreadMessage(
        handle=handle, body=body, timestamp="2026-11-10T12:00:00-07:00",
        outgoing=outgoing, read=False,
    )


def _thread(
    key: str = "t1",
    name: str = "Jordan",
    messages: tuple[ThreadMessage, ...] = (),
    *,
    reply_ready: bool = True,
    is_group: bool = False,
) -> Thread:
    return Thread(
        key=key, name=name, is_group=is_group,
        recipients=("+15551111111", "+15552222222") if is_group else ("+15551111111",),
        reply_ready=reply_ready,
        messages=messages or (_message("h1"),),
        last_ts="2026-11-10T12:00:00-07:00",
    )


def _loaded(**options) -> QuickReplyState:
    state = QuickReplyState("h1")
    assert state.apply_threads([_thread(**options)]) is LoadResult.FOUND
    return state


def test_shows_last_four_messages_oldest_first() -> None:
    messages = tuple(_message(f"h{n}", f"m{n}", outgoing=n % 2 == 0) for n in range(1, 7))
    state = QuickReplyState("h6")
    assert state.apply_threads([_thread(messages=messages)]) is LoadResult.FOUND
    assert MAX_BUBBLES == 4
    assert [bubble.text for bubble in state.bubbles] == ["m3", "m4", "m5", "m6"]
    assert [bubble.outgoing for bubble in state.bubbles] == [False, True, False, True]
    assert state.title == "Jordan" and state.initial == "J"


def test_initial_falls_back_when_the_name_has_no_letter() -> None:
    state = QuickReplyState("h1")
    state.apply_threads([_thread(name="+1 555 111 1111")])
    assert state.initial == "1"
    state = QuickReplyState("h1")
    state.apply_threads([_thread(name="🙂")])
    assert state.initial == "#"


def test_first_miss_reloads_second_miss_falls_back() -> None:
    state = QuickReplyState("h-missing")
    assert state.apply_threads([_thread()]) is LoadResult.RELOAD
    assert state.apply_threads([_thread()]) is LoadResult.FALLBACK
    assert state.thread is None


def test_enter_twice_sends_once() -> None:
    state = _loaded()
    first = state.request_send("on my way")
    assert first.action is SendAction.SEND and first.plan is not None
    assert first.plan.thread is not None and first.plan.thread.key == "t1"
    assert first.plan.body == "on my way"
    assert state.send_state is SendState.SENDING
    assert state.request_send("on my way").action is SendAction.IGNORE


def test_empty_reply_is_ignored() -> None:
    state = _loaded()
    assert state.request_send("   ").action is SendAction.IGNORE
    assert state.send_state is SendState.IDLE


def test_sent_box_ignores_another_enter_and_failed_box_may_retry() -> None:
    state = _loaded()
    state.request_send("one")
    state.send_succeeded()
    assert state.send_state is SendState.SENT
    assert state.request_send("two").action is SendAction.IGNORE
    retry = _loaded()
    retry.request_send("one")
    retry.send_failed()
    assert retry.send_state is SendState.FAILED
    assert retry.request_send("one").action is SendAction.SEND


def test_unconfirmed_group_hands_off_instead_of_sending() -> None:
    state = _loaded(is_group=True)
    decision = state.request_send("hi")
    assert decision.action is SendAction.HANDOFF
    assert decision.message == ReplyDisposition.CONFIRM_GROUP.message
    assert state.send_state is SendState.IDLE


def test_read_only_thread_is_blocked_and_has_note() -> None:
    state = _loaded(reply_ready=False)
    assert state.compose_enabled is False
    assert state.note == ReplyDisposition.READ_ONLY.message
    decision = state.request_send("hi")
    assert decision.action is SendAction.BLOCKED
    assert decision.message == ReplyDisposition.READ_ONLY.message


def test_close_is_deferred_while_sending() -> None:
    state = _loaded()
    state.request_send("x")
    for reason in CloseReason:
        assert not state.may_close(reason)
    state.send_failed()
    assert state.may_close(CloseReason.ESCAPE)


def test_switch_waits_for_in_flight_send() -> None:
    state = _loaded()
    state.request_send("x")
    assert state.request_switch("h-other") is False
    assert state.pending_handle == "h-other"
    idle = _loaded()
    assert idle.request_switch("h-other") is True
    assert idle.handle == "h-other" and idle.thread is None


def test_auto_close_only_when_empty_and_idle() -> None:
    state = _loaded()
    assert state.should_auto_close("", 120)
    assert not state.should_auto_close("draft", 999)
    assert not state.should_auto_close("", 119)
    state.request_send("x")
    assert not state.should_auto_close("", 999)


def test_click_routing_needs_handle_and_preference() -> None:
    assert QUICK_REPLY == "quick_reply_on_click"
    assert click_opens_quick_reply("h1", {QUICK_REPLY: True})
    assert not click_opens_quick_reply("", {QUICK_REPLY: True})
    assert not click_opens_quick_reply("h1", {})
    assert not click_opens_quick_reply("h1", {QUICK_REPLY: "yes"})
