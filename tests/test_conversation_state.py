"""Toolkit-neutral conversation transitions stay deterministic."""
from __future__ import annotations

from dataclasses import replace

import pytest

from blueferry.conversation_state import (
    ConversationSnapshot,
    ConversationState,
    ReplyDisposition,
)
from blueferry.models import BackendStatus, Thread, ThreadMessage


def _thread(
    key: str,
    *,
    handle: str = "",
    group: bool = False,
    named: bool = False,
    reply_ready: bool = True,
    roster_changed: bool = False,
) -> Thread:
    messages = (
        ThreadMessage(
            handle=handle,
            body="hello",
            timestamp="2026-08-15T12:00:00-04:00",
            outgoing=False,
            read=True,
        ),
    ) if handle else ()
    return Thread(
        key=key,
        name=key,
        is_group=group,
        recipients=("+15551111111", "+15552222222") if group else (),
        reply_ready=reply_ready,
        messages=messages,
        last_ts="2026-08-15T12:00:00-04:00",
        group_origin="named" if named else "",
        roster_changed=roster_changed,
        unexpected_sender="Casey" if roster_changed else "",
        roster_warning_id="route:casey" if roster_changed else "",
    )


def test_partial_snapshot_preserves_successful_state_and_selection() -> None:
    state = ConversationState()
    original_status = BackendStatus(daemon=True, map=True)
    threads = (_thread("one"), _thread("two"))
    state.apply_snapshot(ConversationSnapshot(original_status, threads))
    state.selected_key = "two"

    state.apply_snapshot(
        ConversationSnapshot(None, None, ("temporary failure",))
    )

    assert state.status is original_status
    assert state.threads == list(threads)
    assert state.selected_key == "two"
    assert state.error == "temporary failure"


def test_selection_follows_the_same_message_when_thread_key_changes() -> None:
    state = ConversationState()
    state.apply_snapshot(
        ConversationSnapshot(None, (_thread("old", handle="message-1"),))
    )

    state.apply_snapshot(
        ConversationSnapshot(None, (_thread("new", handle="message-1"),))
    )

    assert state.selected_key == "new"


def test_presentations_can_leave_the_initial_selection_empty() -> None:
    state = ConversationState(select_first=False)
    state.apply_snapshot(
        ConversationSnapshot(None, (_thread("one"), _thread("two")))
    )

    assert state.selected is None


def test_reply_plan_enforces_read_only_and_group_confirmation() -> None:
    state = ConversationState()
    direct = _thread("direct", reply_ready=False)
    group = _thread("group", group=True)
    named = _thread("named", group=True, named=True)
    state.apply_snapshot(ConversationSnapshot(None, (direct, group, named)))

    assert state.plan_reply("hello", thread_key="direct").disposition is (
        ReplyDisposition.READ_ONLY
    )
    assert state.plan_reply("hello", thread_key="group").disposition is (
        ReplyDisposition.CONFIRM_GROUP
    )

    plan = state.plan_reply(
        " hello ", thread_key="group", confirm_group=True
    )
    assert plan.ready is True
    assert plan.body == "hello"
    state.reply_sent(plan)
    assert "group" in state.confirmed_groups
    assert state.plan_reply("again", thread_key="group").ready is True
    assert state.plan_reply("again", thread_key="named").disposition is (
        ReplyDisposition.CONFIRM_GROUP
    )


def test_group_update_clears_prior_confirmation() -> None:
    state = ConversationState()
    group = _thread("group", group=True)
    state.apply_snapshot(ConversationSnapshot(None, (group,)))
    state.confirmed_groups.add(group.key)

    updated = replace(group, recipients=("+15553333333", "+15554444444"))
    state.group_participants_saved(updated)

    assert state.selected is updated
    assert group.key not in state.confirmed_groups


def test_roster_warning_is_returned_once_per_stable_warning_id() -> None:
    state = ConversationState()
    warning = _thread("group", group=True, roster_changed=True)
    state.apply_snapshot(ConversationSnapshot(None, (warning,)))

    assert state.next_roster_warning() is warning
    assert state.next_roster_warning() is None

    replacement = replace(warning, roster_warning_id="route:jamie")
    state.apply_snapshot(ConversationSnapshot(None, (replacement,)))
    assert state.next_roster_warning() is replacement


def test_stale_contact_results_cannot_replace_current_query() -> None:
    state = ConversationState()
    old = state.begin_contact_search("Ali")
    current = state.begin_contact_search("Alice")
    assert old is not None and current is not None

    assert state.apply_contact_results(old, [("Alina", "+15551111111")]) is False
    assert state.contact_results == []
    assert state.apply_contact_results(
        current, [("Alice", "+15552222222")]
    ) is True
    assert state.contact_results == [("Alice", "+15552222222")]

    assert state.begin_contact_search("  ") is None
    assert state.contact_results == []


def test_reply_sent_rejects_blocked_plan() -> None:
    state = ConversationState()
    with pytest.raises(ValueError, match="blocked"):
        state.reply_sent(state.plan_reply("hello"))
