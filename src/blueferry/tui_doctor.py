"""Options > Phone > Diagnostics: ``blueferry doctor`` inside the terminal client.

The check runs in a child process on a Textual thread worker, never on the
UI thread. Its state and summary come from :mod:`blueferry.doctor_report`,
shared with the Qt and GTK clients. The report is local diagnostics for the
user's own screen (it names the configured MAC address), shown as plain text
in a read-only, selectable text area with a Copy button.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static, TextArea

from blueferry import doctor_report
from blueferry.doctor_report import DoctorReport, run_doctor
from blueferry.i18n import _
from blueferry.text_safety import terminal_text
from blueferry.tui_tasks import TextualTaskRunner

_LEVEL_CLASS = {"ok": "ok", "warning": "warn", "error": "bad"}


class DoctorScreen(ModalScreen[None]):
    """Runs the check when it opens, then shows the report."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", _("Close"), show=False),
    ]

    def __init__(self, runner: Callable[[], DoctorReport] = run_doctor) -> None:
        super().__init__()
        self._runner = runner
        self.state: dict[str, Any] = doctor_report.idle_state()

    @property
    def text(self) -> str:
        return str(self.state.get("text") or "")

    def compose(self) -> ComposeResult:
        with Vertical(id="doctor-dialog", classes="dialog"):
            yield Static(_("Diagnostics Report"), classes="dialog-title")
            yield Static("", id="doctor-summary")
            yield TextArea(
                "", read_only=True, soft_wrap=True, show_line_numbers=False,
                id="doctor-report",
            )
            yield Static(
                _("Select text with the mouse or Shift+arrows if Copy does not "
                  "reach your clipboard."),
                id="doctor-hint", classes="options-caption",
            )
            with Horizontal(classes="dialog-actions"):
                yield Button(_("Run Again"), id="doctor-run")
                yield Button(_("Copy Report"), id="doctor-copy")
                yield Button(_("Close"), variant="primary", id="doctor-close")

    def on_mount(self) -> None:
        self.run_check()

    def on_unmount(self) -> None:
        # Leaving while the check runs: end the child instead of letting it
        # run into its timeout behind a closed screen.
        if self.state.get("running") is True:
            doctor_report.terminate_running()

    def run_check(self) -> None:
        if self.state.get("running") is True:
            return
        self.state = doctor_report.running_state(self.state)
        self._render_state()

        def done(report: object) -> None:
            self.state = doctor_report.finished_state(self.state, report)

        def failed(message: str) -> None:
            self.state = doctor_report.failed_state(message)

        TextualTaskRunner(self, group="doctor").submit(
            self._runner, done, failed, self._render_state,
        )

    def _render_state(self) -> None:
        running = self.state.get("running") is True
        summary = self.query_one("#doctor-summary", Static)
        if running:
            summary.update(_("Running checks…"))
            summary.set_classes("")
        elif self.state.get("ran") is True:
            view = doctor_report.view(self.state)
            summary.update(Text(view["summary"]))
            summary.set_classes(_LEVEL_CLASS[view["level"]])
        report = self.query_one("#doctor-report", TextArea)
        # The report can quote device names; show it as inert text.
        report.load_text(terminal_text(self.text))
        self.query_one("#doctor-run", Button).disabled = running
        self.query_one("#doctor-copy", Button).disabled = running or not self.text

    @on(Button.Pressed, "#doctor-run")
    def run_button(self) -> None:
        self.run_check()

    @on(Button.Pressed, "#doctor-copy")
    def copy_button(self) -> None:
        self.app.copy_to_clipboard(self.text)
        self.notify(_("Report copied"), markup=False)

    @on(Button.Pressed, "#doctor-close")
    def close_button(self) -> None:
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)
