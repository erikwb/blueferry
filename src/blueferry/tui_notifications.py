"""Recent iPhone app notifications in the Textual client (opt-in).

Terminal counterpart of the Qt and GTK Notifications tabs, on the shared
:class:`blueferry.notification_history.NotificationHistory` model with a
:class:`~blueferry.tui_tasks.TextualTaskRunner`. The list is private and
kept only in the daemon's memory, so it is read only while this screen is
open and forgotten when it closes; a content-free ``NotificationsChanged``
reaches :meth:`NotificationsScreen.invalidated` and coalesces into one
reload. The wording (empty, switched off, failing) is
``phone_link_view.notifications_view``.

Each notification is a row of a list. ``P`` (or the Pin button) pins or
unpins the highlighted one through ``Messages1.SetNotificationPinned``;
pinned rows are marked, rows removed on the iPhone say so and are dimmed,
and so are the rows of apps muted in the Notifications settings
(``ListNotificationApps``, refetched on ``NotificationAppsChanged``). The
labels, limits and errors come from ``notification_pin_view`` and
``notification_apps_view``, shared with the other clients. Every field
comes from the iPhone and is rendered as Rich ``Text`` after
``terminal_text``.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, ClassVar, Protocol

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Button, ListItem, ListView, Static

from blueferry import notification_apps_view, notification_pin_view, phone_link_view
from blueferry.client_tasks import TimerFactory
from blueferry.i18n import _
from blueferry.notification_history import LIMIT, NotificationHistory
from blueferry.text_safety import terminal_text
from blueferry.tui_tasks import TextualTaskRunner, tearing_down, timer_factory


class NotificationsClient(Protocol):
    def notifications(self, limit: int = 50) -> dict: ...

    # Messages1.SetNotificationPinned; returns the new state.
    def set_notification_pinned(self, key: str, pinned: bool) -> bool: ...

    # Messages1.ListNotificationApps (only while the daemon supports muting).
    def notification_apps(self) -> dict: ...


def _plain(value: object) -> str:
    return terminal_text(value).replace("\n", " ")


def notification_text(
    row: Mapping[str, str], extras: Mapping[str, Any] | None = None, *, muted: bool = False,
) -> Text:
    """App and time, the pin and removal marks, then summary and body.

    A row removed on the iPhone or from a muted app is dimmed.
    """
    extras = extras or {}
    dim = extras.get("removed") is True or muted
    text = Text(style="dim" if dim else "")
    if extras.get("pinned_text"):
        text.append(f"{_plain(extras['pinned_text'])}  ·  ", style="bold #fde68a")
    text.append(_plain(row["app"]), style="bold")
    if row["time"]:
        text.append(f"  ·  {_plain(row['time'])}", style="dim")
    if extras.get("removed_text"):
        text.append(f"  ·  {_plain(extras['removed_text'])}", style="italic")
    for part in (row["summary"], row["body"]):
        if part:
            text.append("\n")
            text.append(terminal_text(part))
    return text


def notifications_text(rows: list[dict[str, str]]) -> Text:
    """Every row of :func:`notification_text`, a blank line apart."""
    text = Text()
    for index, row in enumerate(rows):
        if index:
            text.append("\n\n")
        text.append_text(notification_text(row))
    return text


class NotificationItem(ListItem):
    """One notification; keeps its record for the pin action."""

    def __init__(
        self, row: Mapping[str, str], record: Mapping[str, Any], *, muted: bool,
    ) -> None:
        self.row = dict(row)
        self.record = dict(record)
        self.extras = notification_pin_view.pin_row_extras(record)
        self.muted = muted
        classes = "notifications-row"
        if self.extras["removed"] or muted:
            classes += " -dimmed"
        super().__init__(Static(notification_text(row, self.extras, muted=muted)),
                         classes=classes)

    @property
    def key(self) -> str:
        return str(self.extras["key"])


class NotificationsScreen(ModalScreen[str | None]):
    """Dismisses with ``"options"`` when the Notifications options should open."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "Close", show=False),
        Binding("r", "refresh", "Refresh", show=False),
        Binding("P", "toggle_pin", "Pin or unpin", show=False),
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
        self._client = client
        # The last ListNotifications answer, for the pin keys and flags the
        # shared rows leave out. Written by the fetch on the worker thread
        # (one assignment), read on the UI thread.
        self._snapshot: Mapping[str, Any] = {}
        self.history = NotificationHistory(
            runner=TextualTaskRunner(self, group="notifications"),
            timer=timer(self),
            fetch=self._fetch,
            on_change=self._update_view,
        )
        self._mute_supported = notification_apps_view.supported(status)
        self.muted: frozenset[str] = frozenset()
        self._apps_loading = False
        self._apps_again = False
        self._apps_runner = TextualTaskRunner(self, group="notification-apps")
        self._pin_runner = TextualTaskRunner(self, group="notification-pin")
        self.pinning = False
        self.pin_error = ""
        self._pin_error_key = ""  # the notification pin_error is about
        self._shown_key: tuple[tuple[str, ...], str] | None = None
        self._status: Mapping[str, Any] = dict(status)

    def compose(self) -> ComposeResult:
        with Vertical(id="notifications-dialog", classes="dialog"):
            yield Static(_("iPhone notifications"), classes="dialog-title")
            yield Static("", id="notifications-content-hint")
            with Vertical(id="notifications-body"):
                yield Static("", id="notifications-placeholder-title")
                yield Static("", id="notifications-explanation")
                yield ListView(id="notifications-list")
            yield Static("", id="notifications-pin-state", classes="dialog-copy")
            with Horizontal(classes="dialog-actions"):
                yield Button(_("Close"), id="notifications-close")
                yield Button(_("Open Notification Settings"), id="notifications-settings")
                yield Button(_("Pin  P"), id="notifications-pin")
                yield Button(_("Refresh"), variant="primary", id="notifications-refresh")

    def on_mount(self) -> None:
        self._shown = True
        # Access, privacy and phone profile gate every read and every row.
        self.history.sync_status(self._status)
        self.history.watch(True)
        self.load_apps()
        self._update_view()
        self.query_one("#notifications-close", Button).focus()

    def on_unmount(self) -> None:
        self._forget()

    # ---- the app's side -------------------------------------------------

    def status_changed(self, status: Mapping[str, Any]) -> None:
        """A new GetStatus: was the list switched on or off?"""
        supported = notification_apps_view.supported(status)
        if supported != self._mute_supported:
            self._mute_supported = supported
            self.load_apps()
        self._status = dict(status)
        self.history.sync_status(self._status)
        hint = phone_link_view.feature_hints(status).get("notifications", "")
        if hint != self._opt_in_hint:
            self._opt_in_hint = hint
            self._update_view()

    def invalidated(self) -> None:
        """``NotificationsChanged``: one coalesced reload while open."""
        self.history.invalidated()

    def apps_changed(self) -> None:
        """``NotificationAppsChanged``: read which apps are muted again."""
        self.load_apps()

    def _fetch(self, limit: int = LIMIT) -> object:
        """On the worker: ``ListNotifications``, keeping the records."""
        snapshot = self._client.notifications(limit)
        if not self._forgotten:
            self._snapshot = snapshot if isinstance(snapshot, Mapping) else {}
        return snapshot

    def load_apps(self) -> None:
        """Which apps are muted, to dim their rows; nothing if unsupported."""
        if self._forgotten or not self._shown:
            return
        if not self._mute_supported:
            self.muted = frozenset()
            self._update_view()
            return
        if self._apps_loading:
            self._apps_again = True
            return
        self._apps_loading = True

        def loaded(value: object) -> None:
            if not self._apps_again:
                self.muted = notification_apps_view.muted_ids(value)
                self._update_view()

        def failed(_message: str) -> None:
            # Keep the rows dimmed as last known: an unreadable answer is
            # no reason to show every app as unmuted.
            pass

        def finished() -> None:
            self._apps_loading = False
            if self._apps_again:
                self._apps_again = False
                self.load_apps()

        self._apps_runner.submit(self._client.notification_apps, loaded, failed, finished)

    # ---- rendering ------------------------------------------------------

    def _records(self) -> list[Mapping[str, Any]]:
        """The shown records, aligned with their ``notification_rows``."""
        if not self.history.rows:  # failed, switched off or forgotten
            return []
        raw = self._snapshot.get("notifications")
        return [r for r in raw if isinstance(r, Mapping)] if isinstance(raw, list) else []

    @property
    def body_text(self) -> str:
        """Everything the list shows, for tests and screen readers."""
        return "\n\n".join(
            notification_text(item.row, item.extras, muted=item.muted).plain
            for item in self.query_one("#notifications-list", ListView).children
            if isinstance(item, NotificationItem)
        )

    def _update_view(self) -> None:
        if self._forgotten or not self._shown or not self.is_attached:
            return
        records = self._records()
        # The shared model decides whether titles and text may be shown.
        rows = phone_link_view.notification_rows(
            records, content=self.history.info.get("content") is True,
        )
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
        title.update(Text(_plain(view["title"])))
        explanation.update(Text(terminal_text(view["explanation"])))
        title.display = explanation.display = not rows
        self.query_one("#notifications-list", ListView).display = bool(rows)
        # Rebuild the rows only when something they show changed.
        shown = (tuple(sorted(self.muted)), repr(records))
        if shown != self._shown_key:
            self._shown_key = shown

            async def render() -> None:
                try:
                    await self._render_rows(rows, records)
                except NoMatches:
                    # Only while the screen closes; otherwise it is a bug.
                    if not tearing_down(self):
                        raise

            # A function, not a coroutine: a newer render may cancel this one
            # before it starts, and an unstarted coroutine would warn.
            # Textual's overloads infer Never for an async function.
            self.run_worker(render, group="notifications-render", exclusive=True)  # type: ignore[arg-type]
        self._render_pin()

    async def _render_rows(
        self, rows: list[dict[str, str]], records: list[Mapping[str, Any]],
    ) -> None:
        listing = self.query_one("#notifications-list", ListView)
        current = listing.highlighted_child
        key = current.key if isinstance(current, NotificationItem) else ""
        await listing.clear()
        items = [
            NotificationItem(
                row, record, muted=str(record.get("app_id") or "") in self.muted,
            )
            for row, record in zip(rows, records, strict=True)
        ]
        if items:
            await listing.extend(items)
            keys = [item.key for item in items]
            listing.index = keys.index(key) if key and key in keys else 0
        self._render_pin()

    def _selected(self) -> NotificationItem | None:
        item = self.query_one("#notifications-list", ListView).highlighted_child
        return item if isinstance(item, NotificationItem) else None

    def _render_pin(self) -> None:
        if self._forgotten or not self._shown or not self.is_attached:
            return
        button = self.query_one("#notifications-pin", Button)
        item = self._selected()
        listing = self.query_one("#notifications-list", ListView)
        supported = any(
            isinstance(child, NotificationItem) and child.extras["can_pin"]
            for child in listing.children
        )
        button.display = supported and listing.display
        state = self.query_one("#notifications-pin-state", Static)
        limit = notification_pin_view.pin_limit_reached(self._snapshot)
        tooltip = ""
        if item is None or not item.extras["can_pin"]:
            button.disabled = True
        elif limit and not item.extras["pinned"]:
            button.disabled = True
            tooltip = notification_pin_view.pin_limit_text(self._snapshot.get("pins_max"))
        else:
            button.disabled = self.pinning
            tooltip = str(item.extras["pin_tooltip"])
        pinned = bool(item and item.extras["pinned"])
        button.label = _("Unpin  P") if pinned else _("Pin  P")
        # A Text, never a str: Textual parses a str tooltip as markup.
        button.tooltip = Text(_plain(tooltip)) if tooltip else None
        message = self.pin_error
        if not message and limit and item is not None and not item.extras["pinned"]:
            message = notification_pin_view.pin_limit_text(self._snapshot.get("pins_max"))
        state.update(Text(_plain(message)))
        state.display = bool(message) and listing.display

    def _set_pin_error(self, message: str, key: str = "") -> None:
        self.pin_error, self._pin_error_key = message, key

    @on(ListView.Highlighted, "#notifications-list")
    def row_highlighted(self) -> None:
        # An error is about one notification; a rebuild that keeps the
        # highlight on it keeps the error, another row drops it.
        item = self._selected()
        if self.pin_error and (item is None or item.key != self._pin_error_key):
            self._set_pin_error("")
        self._render_pin()

    @on(Button.Pressed, "#notifications-pin")
    def pin_button(self) -> None:
        self.action_toggle_pin()

    def action_toggle_pin(self) -> None:
        """Pin or unpin the highlighted notification."""
        item = self._selected()
        if self.pinning or item is None:
            return
        target = notification_pin_view.pin_target(item.record)
        if target is None:
            return
        key, pinned = target
        if pinned and notification_pin_view.pin_limit_reached(self._snapshot):
            self._set_pin_error(notification_pin_view.pin_limit_text(
                self._snapshot.get("pins_max"),
            ), key)
            self._render_pin()
            return
        self.pinning = True
        self._set_pin_error("")
        self._render_pin()

        # The task runner hands on only str(error); keep the error's
        # D-Bus name here for the shared wording.
        # (written on the worker before ``failed`` runs on the UI thread).
        error_name = ""

        def operation() -> bool:
            nonlocal error_name
            try:
                return self._client.set_notification_pinned(key, pinned)
            except Exception as error:
                error_name = str(getattr(error, "dbus_name", "") or "")
                raise

        def failed(message: str) -> None:
            self._set_pin_error(
                notification_pin_view.pin_error_text(error_name, message), key,
            )

        def finished() -> None:
            self.pinning = False
            # NotificationsChanged follows too; this covers a lost signal.
            self.history.refresh()
            self._render_pin()

        self._pin_runner.submit(operation, None, failed, finished)

    # ---- closing --------------------------------------------------------

    def _forget(self) -> None:
        """Drop the private rows and any pending reload."""
        self._forgotten = True
        self._snapshot = {}
        self.muted = frozenset()
        self.history.watch(False)

    @on(Button.Pressed, "#notifications-refresh")
    def refresh_button(self) -> None:
        self.action_refresh()

    def action_refresh(self) -> None:
        self._set_pin_error("")
        self._render_pin()
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
