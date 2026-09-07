"""Toolkit-neutral conversation state and user-action decisions."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from blueferry.client import BackendError
from blueferry.i18n import _
from blueferry.models import BackendStatus, Thread


@dataclass(frozen=True, slots=True)
class ConversationSnapshot:
    """Independent read results; an absent result and error leave that source alone."""

    status: BackendStatus | None = None
    threads: tuple[Thread, ...] | None = None
    status_error: str = ""
    thread_error: str = ""


class SnapshotClient(Protocol):
    def status(self) -> BackendStatus: ...
    def threads(self, limit: int = 1000) -> list[Thread]: ...


def fetch_conversation_snapshot(
    client: SnapshotClient, *, limit: int = 1000,
) -> ConversationSnapshot:
    """Fetch each independently; None distinguishes failure from an empty result."""
    status_error = thread_error = ""
    status: BackendStatus | None = None
    threads: tuple[Thread, ...] | None = None
    try:
        status = client.status()
    except BackendError as error:
        status_error = str(error)
    try:
        threads = tuple(client.threads(limit))
    except BackendError as error:
        thread_error = str(error)
    return ConversationSnapshot(status, threads, status_error, thread_error)


class ReplyDisposition(str, Enum):
    READY = "ready"
    EMPTY = "empty"
    NO_THREAD = "no-thread"
    READ_ONLY = "read-only"
    CONFIRM_GROUP = "confirm-group"
    STALE_GROUP = "stale-group"

    @property
    def message(self) -> str:
        return {
            ReplyDisposition.NO_THREAD: _("Select a conversation first"),
            ReplyDisposition.READ_ONLY: _("This conversation is read-only"),
            ReplyDisposition.CONFIRM_GROUP: _("Group reply requires participant confirmation"),
            ReplyDisposition.STALE_GROUP: _(
                "The group changed. Review the recipients and send again."
            ),
        }.get(self, "")


@dataclass(frozen=True, slots=True)
class ReplyPlan:
    disposition: ReplyDisposition
    thread: Thread | None
    body: str
    confirm_group: bool = False

    @property
    def ready(self) -> bool:
        return self.disposition is ReplyDisposition.READY

    @property
    def expected_group_token(self) -> str:
        return self.thread.confirmation_token if self.thread else ""


@dataclass(frozen=True, slots=True)
class ContactSearch:
    sequence: int
    query: str


class ConversationState:
    """Shared transitions for native conversation presentations."""

    def __init__(self, *, select_first: bool = True) -> None:
        self.select_first = select_first
        self.threads: list[Thread] = []
        self.status = BackendStatus()
        self.selected_key = ""
        self.error = ""
        self._status_error = ""
        self._thread_error = ""
        self.notice = ""
        self.confirmed_groups: dict[str, str] = {}
        self.warned_roster_changes: set[str] = set()
        self.contact_results: list[tuple[str, str]] = []
        self._contact_sequence = 0
        self._active_contact_search: ContactSearch | None = None

    @property
    def selected(self) -> Thread | None:
        return self.thread(self.selected_key)

    @property
    def selected_index(self) -> int:
        return next(
            (
                index
                for index, thread in enumerate(self.threads)
                if thread.key == self.selected_key
            ),
            0,
        )

    def thread(self, key: str) -> Thread | None:
        return next((thread for thread in self.threads
                     if thread.key == key or key in thread.extra.get("aliases", [])), None)

    def apply_snapshot(self, snapshot: ConversationSnapshot) -> None:
        if snapshot.status is not None:
            self.status = snapshot.status
            self._status_error = ""
        elif snapshot.status_error:
            self._status_error = snapshot.status_error
            self.status = BackendStatus(extra={"error": snapshot.status_error})
        if snapshot.thread_error:
            self._thread_error = snapshot.thread_error
        if snapshot.threads is not None:
            self._thread_error = ""
            previous = self.selected
            previous_handle = (
                previous.messages[-1].handle
                if previous is not None and previous.messages
                else ""
            )
            self.threads = list(snapshot.threads)
            for thread in self.threads:
                if thread.is_group and thread.group_confirmed:
                    self.confirmed_groups[thread.key] = thread.confirmation_token
            keys = {thread.key for thread in self.threads}
            if self.selected_key not in keys:
                alias = self.thread(self.selected_key)
                retained = alias.key if alias is not None else next(
                    (
                        thread.key
                        for thread in self.threads
                        if previous_handle
                        and any(
                            message.handle == previous_handle
                            for message in thread.messages
                        )
                    ),
                    "",
                )
                self.selected_key = retained or (
                    self.threads[0].key
                    if self.select_first and self.threads
                    else ""
                )
        self.error = "; ".join(dict.fromkeys(
            error for error in (self._status_error, self._thread_error) if error
        ))

    def move(self, delta: int) -> None:
        if not self.threads:
            return
        index = min(max(self.selected_index + delta, 0), len(self.threads) - 1)
        self.selected_key = self.threads[index].key
        self.notice = ""

    def select_message(self, handle: str) -> bool:
        for thread in self.threads:
            if any(message.handle == handle for message in thread.messages):
                self.selected_key = thread.key
                return True
        return False

    def plan_reply(
        self,
        body: str,
        *,
        thread_key: str | None = None,
        confirm_group: bool = False,
        expected_group_token: str | None = None,
    ) -> ReplyPlan:
        selected = self.thread(thread_key or self.selected_key)
        message = body.strip()
        if not message:
            return ReplyPlan(ReplyDisposition.EMPTY, selected, "")
        if selected is None:
            return ReplyPlan(ReplyDisposition.NO_THREAD, None, message)
        if expected_group_token is not None and expected_group_token != selected.confirmation_token:
            return ReplyPlan(ReplyDisposition.STALE_GROUP, selected, message)
        if not selected.reply_ready:
            return ReplyPlan(ReplyDisposition.READ_ONLY, selected, message)
        if (
            selected.is_group
            and not confirm_group
            and not selected.group_confirmed
            and self.confirmed_groups.get(selected.key) != selected.confirmation_token
        ):
            return ReplyPlan(
                ReplyDisposition.CONFIRM_GROUP,
                selected,
                message,
            )
        return ReplyPlan(
            ReplyDisposition.READY,
            selected,
            message,
            confirm_group=confirm_group,
        )

    def reply_sent(
        self,
        plan: ReplyPlan,
        *,
        preserve_selection: bool = False,
    ) -> None:
        if not plan.ready or plan.thread is None:
            raise ValueError("cannot complete a blocked reply plan")
        thread = plan.thread
        if thread.is_group and plan.confirm_group:
            self.confirmed_groups[thread.key] = thread.confirmation_token
        if not preserve_selection:
            self.selected_key = thread.key

    def group_participants_saved(self, updated: Thread) -> None:
        self.threads = [
            updated if thread.key == updated.key else thread
            for thread in self.threads
        ]
        self.selected_key = updated.key
        self.confirmed_groups.pop(updated.key, None)

    def next_roster_warning(self) -> Thread | None:
        for thread in self.threads:
            if not thread.roster_changed:
                continue
            warning_id = thread.roster_warning_key
            if warning_id in self.warned_roster_changes:
                continue
            self.warned_roster_changes.add(warning_id)
            return thread
        return None

    def begin_contact_search(self, query: str) -> ContactSearch | None:
        selected = query.strip()
        self._contact_sequence += 1
        self.contact_results = []
        if not selected:
            self._active_contact_search = None
            return None
        request = ContactSearch(self._contact_sequence, selected)
        self._active_contact_search = request
        return request

    def apply_contact_results(
        self,
        request: ContactSearch,
        results: list[tuple[str, str]],
    ) -> bool:
        if request != self._active_contact_search:
            return False
        self.contact_results = list(results)
        return True
