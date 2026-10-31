"""Recent iPhone app notifications in the Textual client (opt-in).

Terminal counterpart of the Qt and GTK Notifications tabs, on the shared
:class:`blueferry.notification_history.NotificationHistory` model with a
:class:`~blueferry.tui_tasks.TextualTaskRunner`. The list is private and
kept only in the daemon's memory, so it is read only while this screen is
open and forgotten when it closes; a content-free ``NotificationsChanged``
reaches :meth:`NotificationsScreen.invalidated` and coalesces into one
reload. The wording (empty, switched off, failing) is
``phone_link_view.notifications_view``. Every field comes from the iPhone
and is rendered as Rich ``Text`` after ``terminal_text``.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, ClassVar, Protocol

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from blueferry import phone_link_view
from blueferry.client_tasks import TimerFactory
from blueferry.i18n import _
from blueferry.notification_history import NotificationHistory
from blueferry.text_safety import terminal_text
from blueferry.tui_tasks import TextualTaskRunner, timer_factory


class NotificationsClient(Protocol):
    def notifications(self, limit: int = 50) -> dict: ...


def _plain(value: object) -> str:
    return terminal_text(value).replace("\n", " ")


def notifications_text(rows: list[dict[str, str]]) -> Text:
    """App and time, then the summary and body of each notification."""
    text = Text()
    for index, row in enumerate(rows):
        if index:
            text.append("\n\n")
        text.append(_plain(row["app"]), style="bold")
        if row["time"]:
            text.append(f"  ·  {_plain(row['time'])}", style="dim")
        for part in (row["summary"], row["body"]):
            if part:
                text.append("\n")
                text.append(terminal_text(part))
    return text


class NotificationsScreen(ModalScreen[str | None]):
    """Dismisses with ``"options"`` when the Notifications options should open."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "Close", show=False),
        Binding("r", "refresh", "Refresh", show=False),
    ]

    def __init__(
        self,
        client: NotificationsClient,
        *,
        status: Mapping[str, Any],
        timer: Callable[[Any], TimerFactory] = timer_factory,
    ) -> None:
        super().__init__()
        self._shown = False  # composed and mounted; widgets exist
        self._opt_in_hint = phone_link_view.feature_hints(status).get("notifications", "")
        self._forgotten = False
        self.history = NotificationHistory(
            runner=TextualTaskRunner(self, group="notifications"),
            timer=timer(self),
            fetch=client.notifications,
            on_change=self._update_view,
        )
        self._status: Mapping[str, Any] = dict(status)

    def compose(self) -> ComposeResult:
        with Vertical(id="notifications-dialog", classes="dialog"):
            yield Static(_("iPhone notifications"), classes="dialog-title")
            yield Static("", id="notifications-content-hint")
            with VerticalScroll(id="notifications-body"):
                yield Static("", id="notifications-placeholder-title")
                yield Static("", id="notifications-explanation")
                yield Static("", id="notifications-list")
            with Horizontal(classes="dialog-actions"):
                yield Button(_("Close"), id="notifications-close")
                yield Button(_("Open Notification Settings"), id="notifications-settings")
                yield Button(_("Refresh"), variant="primary", id="notifications-refresh")

    def on_mount(self) -> None:
        self._shown = True
        # Access, privacy and phone profile gate every read and every row.
        self.history.sync_status(self._status)
        self.history.watch(True)
        self._update_view()
        self.query_one("#notifications-close", Button).focus()

    def on_unmount(self) -> None:
        self._forget()

    # ---- the app's side -------------------------------------------------

    def status_changed(self, status: Mapping[str, Any]) -> None:
        """A new GetStatus: was the list switched on or off?"""
        self._status = dict(status)
        self.history.sync_status(self._status)
        hint = phone_link_view.feature_hints(status).get("notifications", "")
        if hint != self._opt_in_hint:
            self._opt_in_hint = hint
            self._update_view()

    def invalidated(self) -> None:
        """``NotificationsChanged``: one coalesced reload while open."""
        self.history.invalidated()

    # ---- rendering ------------------------------------------------------

    def _update_view(self) -> None:
        if self._forgotten or not self._shown or not self.is_attached:
            return
        rows = self.history.rows
        view = phone_link_view.notifications_view(self.history.info, self._opt_in_hint)
        content = self.query_one("#notifications-content-hint", Static)
        content.update(Text(_plain(view["contentHint"])))
        content.display = bool(view["contentHint"])
        self.query_one("#notifications-refresh", Button).disabled = bool(self._opt_in_hint)
        self.query_one("#notifications-settings", Button).display = bool(
            self._opt_in_hint or view["contentHint"]
        )
        title = self.query_one("#notifications-placeholder-title", Static)
        explanation = self.query_one("#notifications-explanation", Static)
        listing = self.query_one("#notifications-list", Static)
        title.update(Text(_plain(view["title"])))
        explanation.update(Text(terminal_text(view["explanation"])))
        listing.update(notifications_text(rows))
        title.display = explanation.display = not rows
        listing.display = bool(rows)

    # ---- closing --------------------------------------------------------

    def _forget(self) -> None:
        """Drop the private rows and any pending reload."""
        self._forgotten = True
        self.history.watch(False)

    @on(Button.Pressed, "#notifications-refresh")
    def refresh_button(self) -> None:
        self.action_refresh()

    def action_refresh(self) -> None:
        if not self._opt_in_hint:
            self.history.refresh()

    @on(Button.Pressed, "#notifications-settings")
    def settings_button(self) -> None:
        self._forget()
        self.dismiss("options")

    @on(Button.Pressed, "#notifications-close")
    def close_button(self) -> None:
        self.action_close()

    def action_close(self) -> None:
        self._forget()
        self.dismiss(None)
