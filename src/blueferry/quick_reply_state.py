"""Toolkit-neutral decisions for the quick reply box.

Clicking a message popup can open a small box with the last few messages of
its conversation and a reply field instead of the full window. The box sends
only what :class:`~blueferry.conversation_state.ConversationState` would send
from the full window; anything that needs a confirmation there is handed back
to the full window instead.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from blueferry.conversation_state import (
    ConversationSnapshot,
    ConversationState,
    ReplyDisposition,
    ReplyPlan,
)
from blueferry.models import Thread

# The GTK client's own preference (``gtk-ui.json``); absent means off.
QUICK_REPLY = "quick_reply_on_click"
MAX_BUBBLES = 4
IDLE_CLOSE_SECONDS = 120

_HANDOFF = frozenset({
    ReplyDisposition.CONFIRM_GROUP,
    ReplyDisposition.CONFIRM_SENDER,
    ReplyDisposition.NEEDS_PARTICIPANTS,
    ReplyDisposition.STALE_GROUP,
    ReplyDisposition.STALE_SENDER,
})


@dataclass(frozen=True, slots=True)
class Bubble:
    text: str
    outgoing: bool
    handle: str


class SendState(str, Enum):
    IDLE = "idle"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"


class SendAction(str, Enum):
    SEND = "send"
    IGNORE = "ignore"
    # The full window's confirmation flow must handle this reply.
    HANDOFF = "handoff"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class SendDecision:
    action: SendAction
    plan: ReplyPlan | None = None
    message: str = ""


class LoadResult(str, Enum):
    FOUND = "found"
    RELOAD = "reload"
    # Still missing after a reload: open the full window instead.
    FALLBACK = "fallback"


class CloseReason(str, Enum):
    ESCAPE = "escape"
    FOCUS_LOST = "focus-lost"
    IDLE = "idle"
    OPENED_APP = "opened-app"


class QuickReplyState:
    def __init__(self, handle: str) -> None:
        self._handle = handle
        self._conversations = ConversationState(select_first=False)
        self._misses = 0
        self._send_state = SendState.IDLE
        self._plan: ReplyPlan | None = None
        self._pending_handle = ""

    @property
    def handle(self) -> str:
        return self._handle

    @property
    def pending_handle(self) -> str:
        return self._pending_handle

    @property
    def send_state(self) -> SendState:
        return self._send_state

    @property
    def thread(self) -> Thread | None:
        return self._conversations.selected

    @property
    def title(self) -> str:
        return self.thread.name if self.thread is not None else ""

    @property
    def initial(self) -> str:
        return next((char.upper() for char in self.title if char.isalnum()), "#")

    @property
    def bubbles(self) -> tuple[Bubble, ...]:
        if self.thread is None:
            return ()
        return tuple(
            Bubble(message.body, message.outgoing, message.handle)
            for message in self.thread.messages[-MAX_BUBBLES:]
        )

    @property
    def compose_enabled(self) -> bool:
        return self.thread is not None and self.thread.can_compose

    @property
    def note(self) -> str:
        if self.thread is None or self.thread.can_compose:
            return ""
        return ReplyDisposition.READ_ONLY.message

    def apply_threads(self, threads: Sequence[Thread]) -> LoadResult:
        self._conversations.apply_snapshot(ConversationSnapshot(threads=tuple(threads)))
        if self._conversations.select_message(self._handle) or self.thread is not None:
            self._misses = 0
            return LoadResult.FOUND
        self._misses += 1
        return LoadResult.RELOAD if self._misses == 1 else LoadResult.FALLBACK

    def request_send(self, body: str) -> SendDecision:
        if self._send_state in (SendState.SENDING, SendState.SENT) or not body.strip():
            return SendDecision(SendAction.IGNORE)
        thread = self.thread
        plan = self._conversations.plan_reply(
            body, thread_key=thread.key if thread is not None else None,
        )
        if plan.ready:
            self._plan = plan
            self._send_state = SendState.SENDING
            return SendDecision(SendAction.SEND, plan)
        if plan.disposition is ReplyDisposition.EMPTY:
            return SendDecision(SendAction.IGNORE)
        action = SendAction.HANDOFF if plan.disposition in _HANDOFF else SendAction.BLOCKED
        return SendDecision(action, plan, plan.disposition.message)

    def send_succeeded(self) -> None:
        if self._send_state is not SendState.SENDING or self._plan is None:
            return
        self._conversations.reply_sent(self._plan, preserve_selection=True)
        self._send_state = SendState.SENT

    def send_failed(self) -> None:
        if self._send_state is SendState.SENDING:
            self._send_state = SendState.FAILED

    def may_close(self, reason: CloseReason) -> bool:
        return self._send_state is not SendState.SENDING

    def should_auto_close(self, draft: str, idle_seconds: float) -> bool:
        return (
            not draft.strip()
            and self._send_state is not SendState.SENDING
            and idle_seconds >= IDLE_CLOSE_SECONDS
        )

    def request_switch(self, handle: str) -> bool:
        """Show another conversation; deferred while a send is in flight."""
        if self._send_state is SendState.SENDING:
            self._pending_handle = handle
            return False
        self._handle = handle
        self._pending_handle = ""
        self._misses = 0
        self._send_state = SendState.IDLE
        self._plan = None
        self._conversations.selected_key = ""
        return True


def click_opens_quick_reply(handle: str, preferences: Mapping[str, Any]) -> bool:
    """Whether a popup click opens the quick reply box rather than the window."""
    return bool(handle) and preferences.get(QUICK_REPLY) is True
