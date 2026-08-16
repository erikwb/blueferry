"""Toolkit-neutral conversation state and user-action decisions."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from blueferry.models import BackendStatus, Thread


@dataclass(frozen=True, slots=True)
class ConversationSnapshot:
    status: BackendStatus | None
    threads: tuple[Thread, ...] | None
    failures: tuple[str, ...] = ()


class ReplyDisposition(str, Enum):
    READY = "ready"
    EMPTY = "empty"
    NO_THREAD = "no-thread"
    READ_ONLY = "read-only"
    CONFIRM_GROUP = "confirm-group"


@dataclass(frozen=True, slots=True)
class ReplyPlan:
    disposition: ReplyDisposition
    thread: Thread | None
    body: str
    confirm_group: bool = False

    @property
    def ready(self) -> bool:
        return self.disposition is ReplyDisposition.READY


@dataclass(frozen=True, slots=True)
class ContactSearch:
    sequence: int
    query: str


class ConversationState:
    """Shared transitions for native conversation presentations."""

    def __init__(self) -> None:
        self.threads: list[Thread] = []
        self.status = BackendStatus()
        self.selected_key = ""
        self.error = ""
        self.notice = ""
        self.confirmed_groups: set[str] = set()
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
        return next((thread for thread in self.threads if thread.key == key), None)

    def apply_snapshot(self, snapshot: ConversationSnapshot) -> None:
        if snapshot.status is not None:
            self.status = snapshot.status
        if snapshot.threads is not None:
            previous = self.selected
            previous_handle = (
                previous.messages[-1].handle
                if previous is not None and previous.messages
                else ""
            )
            self.threads = list(snapshot.threads)
            keys = {thread.key for thread in self.threads}
            if self.selected_key not in keys:
                retained = next(
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
                    self.threads[0].key if self.threads else ""
                )
        self.error = "; ".join(
            failure for failure in snapshot.failures if failure
        )

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
    ) -> ReplyPlan:
        selected = self.thread(thread_key or self.selected_key)
        message = body.strip()
        if not message:
            return ReplyPlan(ReplyDisposition.EMPTY, selected, "")
        if selected is None:
            return ReplyPlan(ReplyDisposition.NO_THREAD, None, message)
        if not selected.reply_ready:
            return ReplyPlan(ReplyDisposition.READ_ONLY, selected, message)
        if (
            selected.is_group
            and (
                selected.group_origin == "named"
                or selected.key not in self.confirmed_groups
            )
            and not confirm_group
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

    def reply_sent(self, plan: ReplyPlan) -> None:
        if not plan.ready or plan.thread is None:
            raise ValueError("cannot complete a blocked reply plan")
        thread = plan.thread
        if thread.is_group and plan.confirm_group and thread.group_origin != "named":
            self.confirmed_groups.add(thread.key)
        self.selected_key = thread.key

    def group_participants_saved(self, updated: Thread) -> None:
        self.threads = [
            updated if thread.key == updated.key else thread
            for thread in self.threads
        ]
        self.selected_key = updated.key
        self.confirmed_groups.discard(updated.key)

    @staticmethod
    def roster_warning_id(thread: Thread) -> str:
        return (
            thread.roster_warning_id
            or f"{thread.key}:{thread.unexpected_sender or 'unknown'}"
        )

    def next_roster_warning(self) -> Thread | None:
        for thread in self.threads:
            if not thread.roster_changed:
                continue
            warning_id = self.roster_warning_id(thread)
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
