"""Optional-feature settings for the Textual client.

The screen opens with ``o`` and lists one checkbox per opt-in the daemon
reports in ``GetStatus``. Each row is saved through its D-Bus setter in a
worker thread, and the checkboxes follow the daemon's answer rather than
the keypress, like the Qt, GTK and Quickshell settings.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Static

from blueferry.client import BackendError
from blueferry.models import BackendStatus
from blueferry.text_safety import terminal_text


class OptionsClient(Protocol):
    def status(self) -> BackendStatus: ...


@dataclass(frozen=True, slots=True)
class OptIn:
    """One opt-in: its GetStatus key, checkbox label, caption and setter."""

    key: str
    label: str
    caption: str
    save: Callable[[Any, bool], Mapping[str, Any]]


OPT_INS: tuple[OptIn, ...] = (
    OptIn(
        key="contact_photos",
        label="Show contact photos from the iPhone",
        caption=(
            "Photos are kept with your contacts and arrive with the next contact "
            "sync. The terminal can't draw them; other clients and popups show "
            "them. Turning this off erases them."
        ),
        save=lambda client, enabled: client.set_contact_photos(enabled),
    ),
    OptIn(
        key="otp_autocopy",
        label="Copy one-time codes to the clipboard",
        caption=(
            "Only new messages from senders not in your contacts, never group "
            "chats. Turning this off clears a copied code still on the clipboard."
        ),
        save=lambda client, enabled: client.set_otp_autocopy(enabled),
    ),
)


def _one_line(value: object) -> str:
    return terminal_text(value).replace("\n", " ")


class OptionsScreen(ModalScreen[None]):
    """Checkboxes for the opt-in features of the running daemon."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape,o", "close", "Close", show=False),
    ]

    def __init__(self, client: OptionsClient, opt_ins: tuple[OptIn, ...] = OPT_INS) -> None:
        super().__init__()
        self.client = client
        self.opt_ins = opt_ins
        self.values: dict[str, Any] | None = None
        self.pending: set[str] = set()
        self.error = ""

    def compose(self) -> ComposeResult:
        with Vertical(id="options-dialog", classes="dialog"):
            yield Static("Options", classes="dialog-title")
            yield Static("Loading…", id="options-note", classes="dialog-copy")
            for opt_in in self.opt_ins:
                yield Checkbox(opt_in.label, id=f"opt-{opt_in.key}")
                yield Static(opt_in.caption, id=f"opt-{opt_in.key}-caption")
            yield Static("", id="options-error")
            yield Button("Close", id="options-close")

    def on_mount(self) -> None:
        self._render_state()
        self.run_worker(self._load, thread=True, group="options", exit_on_error=False)

    def _load(self) -> None:
        values: dict[str, Any] | None = None
        error = ""
        try:
            values = self.client.status().to_dict()
        except BackendError as failure:
            error = _one_line(failure)
        self.app.call_from_thread(self._loaded, values, error)

    def _loaded(self, values: dict[str, Any] | None, error: str) -> None:
        if values is not None:
            self.values = values
        self.error = error
        self._render_state()

    def _save(self, opt_in: OptIn, enabled: bool) -> None:
        result: Mapping[str, Any] | None = None
        error = ""
        try:
            result = opt_in.save(self.client, enabled)
        except BackendError as failure:
            error = _one_line(failure)
        self.app.call_from_thread(self._saved, opt_in.key, result, error)

    def _saved(self, key: str, result: Mapping[str, Any] | None, error: str) -> None:
        self.pending.discard(key)
        if result is not None and self.values is not None:
            self.values.update(result)
        self.error = error
        self._render_state()

    def _render_state(self) -> None:
        values = self.values or {}
        offered = [opt_in for opt_in in self.opt_ins if opt_in.key in values]
        note = self.query_one("#options-note", Static)
        if self.values is None:
            note.update("Loading…")
        elif not offered:
            note.update("This backend offers no optional features.")
        note.display = self.values is None or not offered
        for opt_in in self.opt_ins:
            checkbox = self.query_one(f"#opt-{opt_in.key}", Checkbox)
            shown = opt_in.key in values
            checkbox.display = shown
            self.query_one(f"#opt-{opt_in.key}-caption").display = shown
            checkbox.disabled = opt_in.key in self.pending
            # Reflect the daemon, not the keypress; programmatic updates must
            # not look like a user toggle.
            with self.prevent(Checkbox.Changed):
                checkbox.value = values.get(opt_in.key) is True
        error = self.query_one("#options-error", Static)
        error.display = bool(self.error)
        error.update(Text(self.error))

    @on(Checkbox.Changed)
    def option_changed(self, event: Checkbox.Changed) -> None:
        opt_in = next(
            (item for item in self.opt_ins if event.checkbox.id == f"opt-{item.key}"), None,
        )
        if opt_in is None or self.values is None or opt_in.key in self.pending:
            return
        if event.value == (self.values.get(opt_in.key) is True):
            return
        self.pending.add(opt_in.key)
        self._render_state()
        enabled = event.value
        self.run_worker(
            lambda: self._save(opt_in, enabled),
            thread=True, group="options", exit_on_error=False,
        )

    @on(Button.Pressed, "#options-close")
    def close_button(self) -> None:
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)
