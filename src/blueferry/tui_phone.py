"""Opt-in iPhone media and recent calls for the Textual client.

A line under the service pills shows what the iPhone plays while media
control is on; ``p``, ``[`` and ``]`` control playback. The recent-calls
screen opens with ``h`` while the call history is on. Every blocking backend
call runs in a worker thread, and iPhone text is rendered as Rich ``Text``
after ``terminal_text``, so it carries neither markup nor terminal controls.
"""
from __future__ import annotations

from typing import ClassVar, Protocol

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from blueferry import phone_overview as overview
from blueferry.client import BackendError
from blueferry.models import CallHistoryEntry
from blueferry.text_safety import terminal_text

MAX_CALLS = 100


class CallHistoryClient(Protocol):
    def call_history(self, limit: int = 200) -> list[CallHistoryEntry]: ...


def _plain(value: object) -> str:
    return terminal_text(value).replace("\n", " ")


def now_playing_text(view: overview.NowPlaying) -> Text:
    """One line: play state, title and the keys that work right now."""
    if not view.visible:
        return Text()
    keys = [
        key for key, command in (("[", "previous"), ("p", "toggle"), ("]", "next"))
        if command in view.commands
    ]
    text = Text(f"{'▶' if view.playing else '⏸'}  ")
    text.append(_plain(view.title))
    if keys:
        text.append(f"   {' '.join(keys)}", style="dim")
    return text


def calls_text(rows: list[overview.CallRow]) -> Text:
    text = Text()
    for index, row in enumerate(rows):
        if index:
            text.append("\n")
        style = "bold red" if row.missed else ""
        text.append(_plain(row.caller), style=style)
        text.append(f"  {_plain(row.detail)}  ·  {_plain(row.time)}", style="dim")
    return text


class RecentCallsScreen(ModalScreen[None]):
    """The iPhone's retained recent calls, newest first (read-only)."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape,h", "close", "Close", show=False),
        Binding("m", "toggle_missed", "Missed only", show=False),
    ]

    def __init__(self, client: CallHistoryClient) -> None:
        super().__init__()
        self._client = client
        self.entries: list[CallHistoryEntry] = []
        self.missed_only = False
        self.error = ""
        self.loaded = False

    def compose(self) -> ComposeResult:
        with Vertical(id="recent-calls-dialog", classes="dialog"):
            yield Static("Recent calls", classes="dialog-title")
            with VerticalScroll(id="recent-calls-body"):
                yield Static("Loading…", id="recent-calls-list", classes="dialog-copy")
            with Horizontal(classes="dialog-actions"):
                yield Button("Missed only  m", id="recent-calls-missed")
                yield Button("Close", variant="primary", id="recent-calls-close")

    def on_mount(self) -> None:
        self.refresh_calls()

    @work(thread=True, exclusive=True, group="recent-calls", exit_on_error=False)
    def refresh_calls(self) -> None:
        try:
            entries = self._client.call_history(MAX_CALLS)
        except BackendError as error:
            self.app.call_from_thread(self._show_error, str(error))
            return
        self.app.call_from_thread(self._show, entries)

    def _show(self, entries: list[CallHistoryEntry]) -> None:
        self.entries, self.error, self.loaded = list(entries), "", True
        self._render_calls()

    def _show_error(self, message: str) -> None:
        self.error, self.loaded = message or "Call history is unavailable", True
        self._render_calls()

    def _render_calls(self) -> None:
        body = self.query_one("#recent-calls-list", Static)
        self.query_one("#recent-calls-missed", Button).label = (
            "All calls  m" if self.missed_only else "Missed only  m"
        )
        if self.error:
            body.update(Text(f"Call history is unavailable: {_plain(self.error)}"))
            return
        rows = overview.call_rows(self.entries, missed_only=self.missed_only)
        if rows:
            body.update(calls_text(rows))
        elif self.loaded:
            body.update(Text("No missed calls" if self.missed_only else "No recent calls"))

    @on(Button.Pressed, "#recent-calls-missed")
    def missed_button(self) -> None:
        self.action_toggle_missed()

    @on(Button.Pressed, "#recent-calls-close")
    def close_button(self) -> None:
        self.dismiss(None)

    def action_toggle_missed(self) -> None:
        self.missed_only = not self.missed_only
        self._render_calls()

    def action_close(self) -> None:
        self.dismiss(None)
