"""Modern terminal client shipped with the backend package."""
from __future__ import annotations

import sys
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Protocol

from gi.repository import GLib
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Input, ListItem, ListView, Static, TextArea

from blueferry.backend_lifecycle import BackendLifecycleError, ensure_backend_current
from blueferry.bus import get_session_bus
from blueferry.client import BackendClient, BackendError
from blueferry.models import BackendStatus, Thread, ThreadMessage
from blueferry.protocol import BUS_NAME, EVENTS_IFACE, OBJECT_PATH
from blueferry.text_safety import terminal_text
from blueferry.time_display import format_message_timestamp

_REFRESH_SECONDS = 15.0
_SIGNAL_PUMP_SECONDS = 0.2
_NARROW_WIDTH = 82
_MAX_INPUT = 4096


class _Client(Protocol):
    def status(self) -> BackendStatus: ...

    def threads(self, limit: int = 1000) -> list[Thread]: ...

    def send_to_thread(
        self, key: str, body: str, *, confirm_group: bool = False,
    ) -> str: ...

    def send(self, recipient: str, body: str) -> str: ...

    def set_group_participants(
        self, thread_key: str, recipients: list[str],
    ) -> Thread: ...


class _Monitor(Protocol):
    def pump(self) -> tuple[bool, str | None]: ...

    def close(self) -> None: ...


def _one_line(value: object) -> str:
    return terminal_text(value).replace("\n", " ")


def _short_timestamp(value: str) -> str:
    formatted = format_message_timestamp(value)
    return formatted.removeprefix("Today at ")


def _initials(name: str) -> str:
    words = [word for word in _one_line(name).split() if word]
    if not words:
        return "?"
    if len(words) == 1:
        initials = words[0][:2].upper()
    else:
        initials = (words[0][0] + words[-1][0]).upper()
    return " ".join(initials)


def _unread_count(thread: Thread) -> int:
    return sum(not message.outgoing and not message.read for message in thread.messages)


@dataclass(frozen=True, slots=True)
class TuiSnapshot:
    status: BackendStatus | None
    threads: tuple[Thread, ...] | None
    failures: tuple[str, ...] = ()


@dataclass
class TuiState:
    client: _Client
    threads: list[Thread] = field(default_factory=list)
    status: BackendStatus = field(default_factory=BackendStatus)
    selected_key: str = ""
    error: str = ""
    notice: str = ""
    confirmed_groups: set[str] = field(default_factory=set)

    @property
    def selected(self) -> Thread | None:
        return next(
            (thread for thread in self.threads if thread.key == self.selected_key),
            None,
        )

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

    def fetch_snapshot(self) -> TuiSnapshot:
        failures: list[str] = []
        status: BackendStatus | None = None
        threads: tuple[Thread, ...] | None = None
        try:
            status = self.client.status()
        except BackendError as error:
            failures.append(str(error))
        try:
            threads = tuple(self.client.threads(200))
        except BackendError as error:
            failures.append(str(error))
        return TuiSnapshot(status, threads, tuple(failures))

    def apply_snapshot(self, snapshot: TuiSnapshot) -> None:
        if snapshot.status is not None:
            self.status = snapshot.status
        if snapshot.threads is not None:
            previous = self.selected_key
            self.threads = list(snapshot.threads)
            keys = {thread.key for thread in self.threads}
            self.selected_key = (
                previous
                if previous in keys
                else (self.threads[0].key if self.threads else "")
            )
        self.error = "; ".join(failure for failure in snapshot.failures if failure)

    def refresh(self) -> None:
        self.apply_snapshot(self.fetch_snapshot())

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
                self.notice = "Opened desktop notification"
                return True
        return False

    def send_reply(
        self,
        body: str,
        *,
        confirm_group: bool = False,
        thread_key: str | None = None,
    ) -> bool:
        thread = next(
            (
                candidate
                for candidate in self.threads
                if candidate.key == (thread_key or self.selected_key)
            ),
            None,
        )
        if thread is None:
            self.error = "Select a conversation first"
            return False
        if not thread.reply_ready:
            self.error = "This conversation is read-only"
            return False
        if (
            thread.is_group
            and (
                thread.extra.get("group_origin") == "named"
                or thread.key not in self.confirmed_groups
            )
            and not confirm_group
        ):
            self.error = "Group reply requires participant confirmation"
            return False
        try:
            self.client.send_to_thread(
                thread.key,
                body,
                confirm_group=confirm_group,
            )
        except BackendError as error:
            self.error = str(error)
            return False
        if (
            thread.is_group
            and confirm_group
            and thread.extra.get("group_origin") != "named"
        ):
            self.confirmed_groups.add(thread.key)
        self.selected_key = thread.key
        self.error = ""
        self.notice = "Message sent"
        self.refresh()
        return True

    def send_new(self, recipient: str, body: str) -> bool:
        try:
            self.client.send(recipient, body)
        except BackendError as error:
            self.error = str(error)
            return False
        self.error = ""
        self.notice = "Message sent"
        self.refresh()
        return True

    def save_group_participants(
        self, thread_key: str, recipients: list[str],
    ) -> bool:
        try:
            updated = self.client.set_group_participants(thread_key, recipients)
        except BackendError as error:
            self.error = str(error)
            return False
        self.threads = [
            updated if thread.key == thread_key else thread
            for thread in self.threads
        ]
        self.selected_key = thread_key
        self.confirmed_groups.discard(thread_key)
        self.error = ""
        self.notice = "Group participants saved locally"
        return True


class _EventMonitor:
    """Bridge GLib-dispatched daemon signals into Textual's event loop."""

    def __init__(self) -> None:
        self.handles: deque[str] = deque()
        self.invalidated = False
        self._context = GLib.MainContext.default()
        bus = get_session_bus()
        common = {
            "dbus_interface": EVENTS_IFACE,
            "bus_name": BUS_NAME,
            "path": OBJECT_PATH,
        }
        self._matches = [
            bus.add_signal_receiver(
                lambda _props: self._invalidate(),
                signal_name="HistoryChanged",
                **common,
            ),
            bus.add_signal_receiver(
                lambda: self._invalidate(),
                signal_name="StatusChanged",
                **common,
            ),
            bus.add_signal_receiver(
                lambda handle: self.handles.append(str(handle)),
                signal_name="OpenMessageRequested",
                **common,
            ),
        ]

    def _invalidate(self) -> None:
        self.invalidated = True

    def pump(self) -> tuple[bool, str | None]:
        while self._context.pending():
            self._context.iteration(False)
        invalidated, self.invalidated = self.invalidated, False
        return invalidated, (self.handles.pop() if self.handles else None)

    def close(self) -> None:
        for match in self._matches:
            match.remove()


class ConversationItem(ListItem):
    def __init__(self, thread: Thread) -> None:
        self.thread_key = thread.key
        latest = thread.messages[-1] if thread.messages else None
        preview = _one_line(latest.body) if latest else "No messages yet"
        unread = _unread_count(thread)
        detail = f"{unread} unread" if unread else ("group" if thread.is_group else "direct")
        avatar = Static(Text(_initials(thread.name), justify="center"), classes="avatar")
        title = Static(Text(_one_line(thread.name), style="bold"), classes="thread-name")
        timestamp = Static(
            Text(_short_timestamp(thread.last_ts), justify="right"),
            classes="thread-time",
        )
        heading = Horizontal(title, timestamp, classes="thread-heading")
        copy = Vertical(
            heading,
            Static(Text(preview), classes="thread-preview"),
            Static(Text(detail), classes="thread-detail unread" if unread else "thread-detail"),
            classes="thread-copy",
        )
        super().__init__(Horizontal(avatar, copy), classes="conversation-item")


class MessageRow(Horizontal):
    def __init__(self, message: ThreadMessage, fallback_name: str) -> None:
        direction = "outgoing" if message.outgoing else "incoming"
        sender = _one_line(
            "You" if message.outgoing else (message.sender or fallback_name)
        )
        meta = f"{sender}  ·  {_short_timestamp(message.timestamp)}"
        bubble = Vertical(
            Static(Text(meta), classes="message-meta"),
            Static(Text(terminal_text(message.body)), classes="message-body"),
            classes=f"message-bubble {direction}",
        )
        super().__init__(bubble, classes=f"message-row {direction}")


class MessageComposer(TextArea):
    """Message editor where Enter sends and Shift+Enter inserts a newline."""

    class Submitted(Message):
        def __init__(self, composer: MessageComposer) -> None:
            super().__init__()
            self.composer = composer

        @property
        def control(self) -> MessageComposer:
            return self.composer

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self.post_message(self.Submitted(self))
            return
        if event.key == "shift+enter":
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        await super()._on_key(event)


class GroupConfirmScreen(ModalScreen[bool]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", show=False)
    ]

    def __init__(self, thread: Thread) -> None:
        super().__init__()
        self.thread = thread

    def compose(self) -> ComposeResult:
        recipients = "\n".join(f"• {_one_line(value)}" for value in self.thread.recipients)
        with Vertical(id="confirm-dialog", classes="dialog"):
            yield Static("Confirm group reply", classes="dialog-title")
            yield Static(
                Text(f"This message will be sent to:\n\n{recipients}"),
                classes="dialog-copy",
            )
            with Horizontal(classes="dialog-actions"):
                yield Button("Cancel", id="confirm-cancel")
                yield Button("Send to group", variant="primary", id="confirm-send")

    @on(Button.Pressed, "#confirm-cancel")
    def cancel_button(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#confirm-send")
    def confirm_button(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class RosterChangedScreen(ModalScreen[tuple[str, ...] | None]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", show=False)
    ]

    def __init__(self, thread: Thread) -> None:
        super().__init__()
        self.thread = thread

    def compose(self) -> ComposeResult:
        sender = str(self.thread.extra.get("unexpected_sender") or "Someone new")
        if self.thread.extra.get("roster_changed"):
            title = "Group membership may have changed"
            copy = (
                f"{sender} sent a message to {self.thread.name}, but is not in "
                "BlueFerry's saved participant list. Replies are disabled until "
                "you review it. This may also mean you have multiple groups with "
                f"the name {self.thread.name}, which BlueFerry cannot distinguish."
            )
        else:
            title = "Edit group participants"
            copy = (
                "BlueFerry uses this local list when replying to "
                f"{self.thread.name}. Groups with the same name cannot be "
                "distinguished."
            )
        copy += (
            "\n\nChanging BlueFerry's saved list does not add or remove anyone "
            "in Messages on your iPhone."
        )
        with Vertical(id="roster-changed-dialog", classes="dialog"):
            yield Static(title, classes="dialog-title")
            yield Static(Text(terminal_text(copy)), classes="dialog-copy")
            yield Static("BlueFerry participant list", classes="field-label")
            yield TextArea(
                soft_wrap=False,
                show_line_numbers=False,
                id="roster-participants",
            )
            with Horizontal(classes="dialog-actions"):
                yield Button("Not now", id="roster-changed-cancel")
                yield Button(
                    "Save participants", variant="primary",
                    id="roster-changed-save",
                )

    def on_mount(self) -> None:
        editor = self.query_one("#roster-participants", TextArea)
        editor.text = "\n".join(self.thread.recipients)
        editor.focus()

    @on(Button.Pressed, "#roster-changed-cancel")
    def cancel_button(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#roster-changed-save")
    def save_button(self) -> None:
        recipients: list[str] = []
        for line in self.query_one("#roster-participants", TextArea).text.splitlines():
            address = line.strip()
            if address and address not in recipients:
                recipients.append(address)
        if not 2 <= len(recipients) <= 20:
            self.notify(
                "Enter 2 to 20 participants, one per line",
                severity="warning",
            )
            return
        self.dismiss(tuple(recipients))

    def action_cancel(self) -> None:
        self.dismiss(None)


class NewMessageScreen(ModalScreen[tuple[str, str] | None]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="new-message-dialog", classes="dialog"):
            yield Static("New message", classes="dialog-title")
            yield Static("Phone number or email", classes="field-label")
            yield Input(placeholder="+1 555 123 4567", id="new-recipient")
            yield Static("Message", classes="field-label")
            yield MessageComposer(
                placeholder="Write a message…",
                soft_wrap=True,
                show_line_numbers=False,
                id="new-body",
            )
            with Horizontal(classes="dialog-actions"):
                yield Button("Cancel", id="new-cancel")
                yield Button("Send message", variant="primary", id="new-send")

    def on_mount(self) -> None:
        self.query_one("#new-recipient", Input).focus()

    @on(Button.Pressed, "#new-cancel")
    def cancel_button(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#new-send")
    def send_button(self) -> None:
        self.action_submit()

    @on(MessageComposer.Submitted, "#new-body")
    def message_submitted(self) -> None:
        self.action_submit()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_submit(self) -> None:
        recipient = self.query_one("#new-recipient", Input).value.strip()
        body = self.query_one("#new-body", TextArea).text.strip()
        if not recipient or not body:
            self.notify("Add both a recipient and a message", severity="warning")
            return
        if len(body) > _MAX_INPUT:
            self.notify(f"Messages are limited to {_MAX_INPUT} characters", severity="error")
            return
        self.dismiss((recipient, body))


class HelpScreen(ModalScreen[None]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape,question_mark", "close", "Close", show=False),
    ]

    def compose(self) -> ComposeResult:
        help_text = Text.from_markup(
            "[bold #7dd3fc]Move[/]  ↑ ↓ or j k\n"
            "[bold #7dd3fc]Open / reply[/]  Enter\n"
            "[bold #7dd3fc]Send[/]  Enter\n"
            "[bold #7dd3fc]New line[/]  Shift+Enter\n"
            "[bold #7dd3fc]Search[/]  /\n"
            "[bold #7dd3fc]New message[/]  n\n"
            "[bold #7dd3fc]Commands[/]  Ctrl+P\n"
            "[bold #7dd3fc]Refresh[/]  r\n"
            "[bold #7dd3fc]Back[/]  Esc\n"
            "[bold #7dd3fc]Quit[/]  q"
        )
        with Vertical(id="help-dialog", classes="dialog"):
            yield Static("Keyboard map", classes="dialog-title")
            yield Static(help_text, classes="dialog-copy")
            yield Button("Got it", variant="primary", id="help-close")

    @on(Button.Pressed, "#help-close")
    def close_button(self) -> None:
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)


class BlueFerryApp(App[None]):
    TITLE = "BlueFerry"
    SUB_TITLE = "Messages over Bluetooth"
    CSS_PATH = Path(__file__).with_name("tui.tcss")
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q", "quit", "Quit"),
        Binding("n", "new_message", "New"),
        Binding("/", "focus_search", "Search"),
        Binding("r", "refresh", "Refresh"),
        Binding("question_mark", "help", "Help"),
        Binding("j,down", "next_thread", "Next", show=False),
        Binding("k,up", "previous_thread", "Previous", show=False),
        Binding("enter", "open_thread", "Open", show=False),
        Binding("escape", "return_to_list", "Back", show=False),
    ]

    def __init__(
        self,
        state: TuiState,
        *,
        monitor_factory: Callable[[], _Monitor | None] = _EventMonitor,
    ) -> None:
        super().__init__()
        self.state = state
        self._monitor_factory = monitor_factory
        self._monitor: _Monitor | None = None
        self._pending_open_handle: str | None = None
        self._sending = False
        self._warned_roster_changes: set[str] = set()

    def compose(self) -> ComposeResult:
        with Horizontal(id="masthead"):
            yield Static(Text("●  BLUEFERRY", style="bold #7dd3fc"), id="brand")
            yield Static("Starting…", id="connection-summary")
            yield Static("", id="release")
        with Horizontal(id="service-strip"):
            yield Static("MESSAGES  …", id="map-status", classes="status-pill")
            yield Static("CONTACTS  …", id="pbap-status", classes="status-pill")
            yield Static("NOTIFICATIONS  …", id="ancs-status", classes="status-pill")
            yield Static("STORAGE  …", id="storage-status", classes="status-pill")
        with Horizontal(id="workspace"):
            with Vertical(id="sidebar"):
                with Horizontal(id="sidebar-heading"):
                    yield Static("Conversations", id="sidebar-title")
                    yield Button("+", id="new-message", tooltip="New message")
                yield Input(placeholder="Search conversations  /", id="thread-search")
                yield ListView(id="thread-list")
            with Vertical(id="conversation"):
                with Horizontal(id="conversation-heading"):
                    yield Button("<", id="back-to-list", tooltip="Back to conversations")
                    with Vertical(id="conversation-title-wrap"):
                        yield Static("Select a conversation", id="conversation-title")
                        yield Static("", id="conversation-subtitle")
                    yield Button(
                        "Participants",
                        id="edit-group-participants",
                        tooltip="Edit BlueFerry's local participant list",
                    )
                    yield Static("", id="conversation-badge")
                yield VerticalScroll(
                    Static("Choose a conversation to start", id="empty-conversation"),
                    id="message-timeline",
                )
                with Vertical(id="composer-wrap"):
                    yield MessageComposer(
                        placeholder="Write a reply…",
                        soft_wrap=True,
                        show_line_numbers=False,
                        id="composer",
                    )
                    with Horizontal(id="composer-meta"):
                        yield Static("Enter to send  ·  Shift+Enter for new line", id="send-hint")
                        yield Static(f"0 / {_MAX_INPUT}", id="character-count")
                        yield Button("Send  ↵", variant="primary", id="send-reply")
        yield Static("Ready", id="notice-bar")
        yield Footer(show_command_palette=False)

    def on_mount(self) -> None:
        try:
            self._monitor = self._monitor_factory()
        except Exception:
            self._monitor = None
        self.set_interval(_SIGNAL_PUMP_SECONDS, self._pump_events)
        self.set_interval(_REFRESH_SECONDS, self._load_data)
        self._update_responsive(self.size.width)
        self._load_data()

    def on_unmount(self) -> None:
        if self._monitor is not None:
            self._monitor.close()

    def on_resize(self, event: events.Resize) -> None:
        self._update_responsive(event.size.width)

    def _update_responsive(self, width: int) -> None:
        workspace = self.query_one("#workspace", Horizontal)
        workspace.set_class(width < _NARROW_WIDTH, "narrow")
        if width >= _NARROW_WIDTH:
            workspace.remove_class("chat-open")

    def _pump_events(self) -> None:
        if self._monitor is None:
            return
        invalidated, handle = self._monitor.pump()
        if handle:
            self._pending_open_handle = handle
            if self.state.select_message(handle):
                self._pending_open_handle = None
                self.call_later(self._show_selected_from_notification)
            else:
                invalidated = True
        if invalidated:
            self._load_data()

    async def _show_selected_from_notification(self) -> None:
        await self._populate_threads()
        await self._render_conversation()
        self.query_one("#workspace").add_class("chat-open")
        self._update_notice()

    @work(thread=True, exclusive=True, group="refresh", exit_on_error=False)
    def _load_data(self) -> None:
        snapshot = self.state.fetch_snapshot()
        try:
            self.call_from_thread(self._schedule_snapshot, snapshot)
        except Exception as error:
            self.state.error = f"Could not update terminal view: {_one_line(error)}"
            self.call_from_thread(self._update_notice)

    def _schedule_snapshot(self, snapshot: TuiSnapshot) -> None:
        self.run_worker(
            self._apply_snapshot(snapshot),
            name="render-snapshot",
            group="render",
            exclusive=True,
        )

    async def _apply_snapshot(self, snapshot: TuiSnapshot) -> None:
        previous_status = self.state.status
        previous_threads = tuple(self.state.threads)
        previous_selection = self.state.selected_key
        previous_error = self.state.error
        self.state.apply_snapshot(snapshot)
        if self._pending_open_handle and self.state.select_message(self._pending_open_handle):
            self._pending_open_handle = None
            self.query_one("#workspace").add_class("chat-open")
        threads_changed = tuple(self.state.threads) != previous_threads
        selection_changed = self.state.selected_key != previous_selection
        if threads_changed or selection_changed:
            await self._populate_threads()
            await self._render_conversation()
        if self.state.status != previous_status:
            self._update_status()
        if self.state.error != previous_error:
            self._update_notice()
        if threads_changed:
            self._warn_about_roster_changes()

    def _warn_about_roster_changes(self) -> None:
        for thread in self.state.threads:
            if not thread.extra.get("roster_changed"):
                continue
            warning_id = str(
                thread.extra.get("roster_warning_id")
                or f"{thread.key}:{thread.extra.get('unexpected_sender') or 'unknown'}"
            )
            if warning_id in self._warned_roster_changes:
                continue
            self._warned_roster_changes.add(warning_id)
            self._open_roster_editor(thread)
            return

    def _open_roster_editor(self, thread: Thread) -> None:
        self.push_screen(
            RosterChangedScreen(thread),
            lambda recipients, key=thread.key: self._roster_review_ready(
                key, recipients
            ),
        )

    def _roster_review_ready(
        self, thread_key: str, recipients: tuple[str, ...] | None,
    ) -> None:
        if recipients is None:
            return
        self.state.notice = "Saving group participants…"
        self._update_notice()
        self._save_group_participants_worker(thread_key, list(recipients))

    def _filtered_threads(self) -> list[Thread]:
        query = self.query_one("#thread-search", Input).value.strip().casefold()
        if not query:
            return self.state.threads
        selected: list[Thread] = []
        for thread in self.state.threads:
            preview = thread.messages[-1].body if thread.messages else ""
            haystack = " ".join((thread.name, *thread.recipients, preview)).casefold()
            if query in haystack:
                selected.append(thread)
        return selected

    async def _populate_threads(self) -> None:
        if not self.is_running:
            return
        try:
            thread_list = self.query_one("#thread-list", ListView)
        except NoMatches:
            return
        visible = self._filtered_threads()
        await thread_list.clear()
        if visible:
            await thread_list.extend(ConversationItem(thread) for thread in visible)
            selected_index = next(
                (
                    index
                    for index, thread in enumerate(visible)
                    if thread.key == self.state.selected_key
                ),
                0,
            )
            thread_list.index = selected_index

    async def _render_conversation(self) -> None:
        if not self.is_running:
            return
        thread = self.state.selected
        try:
            timeline = self.query_one("#message-timeline", VerticalScroll)
            composer = self.query_one("#composer", TextArea)
            send_button = self.query_one("#send-reply", Button)
            roster_button = self.query_one("#edit-group-participants", Button)
        except NoMatches:
            return
        await timeline.remove_children()
        if thread is None:
            self.query_one("#conversation-title", Static).update("Select a conversation")
            self.query_one("#conversation-subtitle", Static).update("")
            self.query_one("#conversation-badge", Static).update("")
            await timeline.mount(
                Static("Choose a conversation to start", id="empty-conversation")
            )
            composer.disabled = True
            send_button.disabled = True
            roster_button.display = False
            return

        title = thread.name + ("  ·  Group" if thread.is_group else "")
        recipients = ", ".join(thread.recipients)
        self.query_one("#conversation-title", Static).update(Text(_one_line(title)))
        self.query_one("#conversation-subtitle", Static).update(Text(_one_line(recipients)))
        unread = _unread_count(thread)
        self.query_one("#conversation-badge", Static).update(
            Text(f"{unread} unread" if unread else "up to date")
        )
        roster_button.display = bool(
            thread.extra.get("group_origin") == "named"
            or thread.key.startswith("group:named:")
        )

        widgets: list[Static | MessageRow] = []
        previous_day = ""
        for message in thread.messages:
            timestamp = format_message_timestamp(message.timestamp)
            day = timestamp.partition(" at ")[0]
            if day and day != previous_day:
                widgets.append(Static(Text(day, justify="center"), classes="date-separator"))
                previous_day = day
            widgets.append(MessageRow(message, thread.name))
        if widgets:
            await timeline.mount(*widgets)
        else:
            await timeline.mount(Static("No messages yet", id="empty-conversation"))
        composer.disabled = self._sending or not thread.reply_ready
        composer.placeholder = (
            "Write a reply…" if thread.reply_ready else "This conversation is read-only"
        )
        send_button.disabled = composer.disabled
        timeline.scroll_end(animate=False, immediate=True)

    def _update_status(self) -> None:
        if not self.is_running:
            return
        status = self.state.status
        if status.map:
            summary, summary_class = "iPhone connected", "ok"
        elif status.initializing:
            summary, summary_class = "Connecting to iPhone…", "warn"
        else:
            summary, summary_class = "iPhone offline", "bad"
        connection = self.query_one("#connection-summary", Static)
        connection.update(summary)
        connection.set_classes(summary_class)
        self.query_one("#release", Static).update(f"backend {status.backend_release}")
        self._set_pill("#map-status", "MESSAGES", status.map)
        self._set_pill("#pbap-status", "CONTACTS", status.pbap)
        self._set_pill("#ancs-status", "NOTIFICATIONS", status.ancs)
        storage_ok = status.storage_state in {"ready", "unlocked", "plaintext"}
        storage = self.query_one("#storage-status", Static)
        storage.update(f"STORAGE  {status.storage_state.upper()}")
        storage.set_classes(f"status-pill {'ok' if storage_ok else 'warn'}")

    def _set_pill(self, selector: str, label: str, active: bool) -> None:
        pill = self.query_one(selector, Static)
        pill.update(f"{label}  {'ON' if active else 'OFF'}")
        pill.set_classes(f"status-pill {'ok' if active else 'bad'}")

    def _update_notice(self) -> None:
        if not self.is_running:
            return
        try:
            notice = self.query_one("#notice-bar", Static)
        except NoMatches:
            return
        if self.state.error:
            notice.update(f"!  {self.state.error}")
            notice.set_classes("error")
        elif self.state.notice:
            notice.update(f"✓  {self.state.notice}")
            notice.set_classes("success")
        else:
            notice.update("Ready  ·  Ctrl+P: Help")
            notice.set_classes("")

    @on(ListView.Highlighted, "#thread-list")
    async def thread_highlighted(self, event: ListView.Highlighted) -> None:
        if not isinstance(event.item, ConversationItem):
            return
        self.state.selected_key = event.item.thread_key
        self.state.notice = ""
        await self._render_conversation()
        self._update_notice()

    @on(ListView.Selected, "#thread-list")
    def thread_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, ConversationItem):
            self.state.selected_key = event.item.thread_key
        self.action_open_thread()

    @on(Input.Changed, "#thread-search")
    async def search_changed(self) -> None:
        await self._populate_threads()

    @on(TextArea.Changed, "#composer")
    def composer_changed(self, event: TextArea.Changed) -> None:
        length = len(event.text_area.text)
        counter = self.query_one("#character-count", Static)
        counter.update(f"{length} / {_MAX_INPUT}")
        counter.set_class(length > _MAX_INPUT, "over-limit")

    @on(MessageComposer.Submitted, "#composer")
    def composer_submitted(self) -> None:
        self.action_send_reply()

    @on(Button.Pressed, "#new-message")
    def new_message_button(self) -> None:
        self.action_new_message()

    @on(Button.Pressed, "#back-to-list")
    def back_button(self) -> None:
        self.action_return_to_list()

    @on(Button.Pressed, "#edit-group-participants")
    def edit_group_participants_button(self) -> None:
        thread = self.state.selected
        if thread is not None:
            self._open_roster_editor(thread)

    @on(Button.Pressed, "#send-reply")
    def send_button(self) -> None:
        self.action_send_reply()

    def action_refresh(self) -> None:
        self.state.notice = "Refreshing…"
        self._update_notice()
        self._load_data()

    def action_next_thread(self) -> None:
        thread_list = self.query_one("#thread-list", ListView)
        if thread_list.index is not None and thread_list.children:
            thread_list.index = min(thread_list.index + 1, len(thread_list.children) - 1)
        thread_list.focus()

    def action_previous_thread(self) -> None:
        thread_list = self.query_one("#thread-list", ListView)
        if thread_list.index is not None:
            thread_list.index = max(thread_list.index - 1, 0)
        thread_list.focus()

    def action_open_thread(self) -> None:
        if self.state.selected is None:
            return
        self.query_one("#workspace").add_class("chat-open")
        composer = self.query_one("#composer", TextArea)
        if not composer.disabled:
            composer.focus()

    def action_focus_search(self) -> None:
        self.query_one("#workspace").remove_class("chat-open")
        self.query_one("#thread-search", Input).focus()

    def action_return_to_list(self) -> None:
        workspace = self.query_one("#workspace")
        if workspace.has_class("chat-open"):
            workspace.remove_class("chat-open")
        self.query_one("#thread-list", ListView).focus()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_new_message(self) -> None:
        self.push_screen(NewMessageScreen(), self._new_message_ready)

    def _new_message_ready(self, result: tuple[str, str] | None) -> None:
        if result is None:
            return
        recipient, body = result
        self._set_sending(True, "Sending new message…")
        self._send_new_worker(recipient, body)

    def action_send_reply(self) -> None:
        if self._sending:
            return
        thread = self.state.selected
        body = self.query_one("#composer", TextArea).text.strip()
        if thread is None or not body:
            self.notify("Write a message first", severity="warning")
            return
        if len(body) > _MAX_INPUT:
            self.notify(f"Messages are limited to {_MAX_INPUT} characters", severity="error")
            return
        if thread.is_group and (
            thread.extra.get("group_origin") == "named"
            or thread.key not in self.state.confirmed_groups
        ):
            self.push_screen(
                GroupConfirmScreen(thread),
                lambda confirmed: self._group_reply_ready(confirmed, thread.key, body),
            )
            return
        self._begin_reply(thread.key, body, False)

    def _group_reply_ready(self, confirmed: bool, thread_key: str, body: str) -> None:
        if confirmed:
            self._begin_reply(thread_key, body, True)
        else:
            self.state.notice = "Group reply cancelled"
            self._update_notice()

    def _begin_reply(self, thread_key: str, body: str, confirm_group: bool) -> None:
        self._set_sending(True, "Sending…")
        self._send_reply_worker(thread_key, body, confirm_group)

    def _set_sending(self, sending: bool, notice: str = "") -> None:
        self._sending = sending
        self.state.notice = notice
        thread = self.state.selected
        disabled = sending or thread is None or not thread.reply_ready
        self.query_one("#composer", TextArea).disabled = disabled
        self.query_one("#send-reply", Button).disabled = disabled
        self._update_notice()

    @work(thread=True, group="send", exit_on_error=False)
    def _send_reply_worker(
        self, thread_key: str, body: str, confirm_group: bool,
    ) -> None:
        succeeded = self.state.send_reply(
            body,
            confirm_group=confirm_group,
            thread_key=thread_key,
        )
        self.call_from_thread(self._schedule_send_finished, succeeded, body)

    @work(thread=True, group="send", exit_on_error=False)
    def _send_new_worker(self, recipient: str, body: str) -> None:
        succeeded = self.state.send_new(recipient, body)
        self.call_from_thread(self._schedule_send_finished, succeeded, "")

    @work(thread=True, group="roster", exclusive=True, exit_on_error=False)
    def _save_group_participants_worker(
        self, thread_key: str, recipients: list[str],
    ) -> None:
        succeeded = self.state.save_group_participants(thread_key, recipients)
        self.call_from_thread(self._schedule_roster_finished, succeeded)

    def _schedule_roster_finished(self, succeeded: bool) -> None:
        self.run_worker(
            self._roster_finished(succeeded),
            name="render-roster-result",
            group="render",
            exclusive=True,
        )

    async def _roster_finished(self, succeeded: bool) -> None:
        await self._populate_threads()
        await self._render_conversation()
        self._update_notice()
        if not succeeded:
            thread = self.state.selected
            if thread is not None:
                self.notify(
                    self.state.error or "Could not save group participants",
                    severity="error",
                )
                self._open_roster_editor(thread)

    def _schedule_send_finished(self, succeeded: bool, sent_body: str) -> None:
        self.run_worker(
            self._send_finished(succeeded, sent_body),
            name="render-send-result",
            group="render",
            exclusive=True,
        )

    async def _send_finished(self, succeeded: bool, sent_body: str) -> None:
        self._sending = False
        composer = self.query_one("#composer", TextArea)
        if succeeded and sent_body and composer.text.strip() == sent_body:
            composer.text = ""
        await self._populate_threads()
        await self._render_conversation()
        self._update_status()
        self._update_notice()


def main() -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("blueferry-tui requires an interactive terminal", file=sys.stderr)
        return 2
    try:
        ensure_backend_current()
    except (BackendLifecycleError, RuntimeError) as error:
        print(f"BlueFerry backend unavailable: {_one_line(error)}", file=sys.stderr)
        print("Run 'blueferry pair-setup' first if no phone is configured.", file=sys.stderr)
        return 2

    try:
        BlueFerryApp(TuiState(BackendClient())).run()
    except KeyboardInterrupt:
        return 130
    except (OSError, RuntimeError) as error:
        print(f"Could not initialize terminal UI: {_one_line(error)}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
