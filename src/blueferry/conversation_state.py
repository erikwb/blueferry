"""Toolkit-neutral conversation state and user-action decisions."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from blueferry.client import BackendError
from blueferry.events import canonical_address
from blueferry.i18n import _
from blueferry.models import BackendStatus, Thread
from blueferry.recipients import (
    SENDER_REPLY_TOKEN_PREFIX,
    InvalidRecipient,
    validate_recipient,
)

MAX_GROUP_PARTICIPANTS = 20


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
    # The client asks who is in the group, then plans the reply again.
    NEEDS_PARTICIPANTS = "needs-participants"
    # The latest message may be from a group; the client asks whether the
    # reply goes to its sender alone, then plans it with confirm_group.
    CONFIRM_SENDER = "confirm-sender"
    STALE_SENDER = "stale-sender"

    @property
    def message(self) -> str:
        return {
            ReplyDisposition.NO_THREAD: _("Select a conversation first"),
            ReplyDisposition.READ_ONLY: _("This conversation is read-only"),
            ReplyDisposition.CONFIRM_GROUP: _("Group reply requires participant confirmation"),
            ReplyDisposition.STALE_GROUP: _(
                "The group changed. Review the recipients and send again."
            ),
            ReplyDisposition.NEEDS_PARTICIPANTS: _(
                "Confirm who is in this group before replying"
            ),
            ReplyDisposition.CONFIRM_SENDER: _(
                "Confirm that this reply goes only to the sender"
            ),
            ReplyDisposition.STALE_SENDER: _(
                "A new message arrived. Review the conversation and send again."
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
        self.status = BackendStatus(storage_policy="", storage_state="unavailable")
        self.selected_key = ""
        self.error = ""
        self._status_error = ""
        self._thread_error = ""
        self.notice = ""
        # Thread key to the confirmation token of a group roster, or of a
        # reply to the sender alone, that the user approved this session.
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
            self.status = BackendStatus(
                storage_policy=self.status.storage_policy,
                storage_state="unavailable",
                extra={"error": snapshot.status_error},
            )
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
            # A confirmed sender-only reply went stale because a message arrived.
            return ReplyPlan(
                ReplyDisposition.STALE_SENDER
                if expected_group_token.startswith(SENDER_REPLY_TOKEN_PREFIX)
                else ReplyDisposition.STALE_GROUP,
                selected, message,
            )
        if selected.is_group and selected.participants_required:
            return ReplyPlan(ReplyDisposition.NEEDS_PARTICIPANTS, selected, message)
        if selected.sender_reply_token and not selected.reply_ready:
            if (
                confirm_group
                or self.confirmed_groups.get(selected.key) == selected.sender_reply_token
            ):
                return ReplyPlan(
                    ReplyDisposition.READY, selected, message, confirm_group=True,
                )
            return ReplyPlan(ReplyDisposition.CONFIRM_SENDER, selected, message)
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
        if plan.confirm_group and (thread.is_group or thread.sender_reply_token):
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

    def contact_result_rows(self) -> list[dict[str, str]]:
        """The current contact matches as plain ``name``/``address`` rows."""
        return [
            {"name": str(name), "address": str(address)}
            for name, address in self.contact_results
        ]


class ParticipantDraft:
    """The confirm-members editor's list, shared by the GTK and terminal clients.

    It starts with the addresses BlueFerry already knows. The backend still
    validates the saved list; this only keeps the editor's rows sensible.
    """

    def __init__(self, thread: Thread) -> None:
        self.thread = thread
        self.entries: list[tuple[str, str]] = []
        for name, address in thread.participant_draft:
            self._append(name, address)

    def _identity(self, address: str) -> str:
        return canonical_address(address) or address.strip().casefold()

    def _append(self, name: str, address: str) -> None:
        identity = self._identity(address)
        if all(self._identity(present) != identity for _name, present in self.entries):
            self.entries.append((name, address))

    def add(self, value: str, name: str = "") -> bool:
        """Add a phone number or email; False if it isn't one or the list is full."""
        try:
            address = validate_recipient(value)
        except InvalidRecipient:
            return False
        if len(self.entries) >= MAX_GROUP_PARTICIPANTS:
            return False
        self._append(name, address)
        return True

    def remove(self, address: str) -> None:
        self.entries = [entry for entry in self.entries if entry[1] != address]

    @property
    def recipients(self) -> list[str]:
        return [address for _name, address in self.entries]

    @property
    def can_save(self) -> bool:
        return 2 <= len(self.entries) <= MAX_GROUP_PARTICIPANTS

    @property
    def missing(self) -> list[str]:
        named = {name.casefold() for name, _address in self.entries if name}
        return [member for member in self.thread.missing_members if member.casefold() not in named]

    @property
    def hint(self) -> str:
        missing = self.missing
        return _("Still needed: {names}").format(names=", ".join(missing)) if missing else ""

    def suggestions(self, results: list[tuple[str, str]]) -> list[tuple[str, str]]:
        """Contact matches not already in the list, one row per address."""
        present = {self._identity(address) for _name, address in self.entries}
        return [
            (name, address) for name, address in results
            if self._identity(address) not in present
        ]
