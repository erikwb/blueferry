"""The iPhone overview, opt-in now playing and recent calls in the Textual client.

- A line under the service pills shows what the iPhone plays while media
  control is on; ``p``, ``[`` and ``]`` control playback.
- :class:`PhoneOverviewScreen` (command palette "iPhone Overview") is the
  terminal's counterpart of the Qt and GTK phone card: the phone's name,
  connection, battery, signal, network and now playing, and for each opt-in
  feature that is off the setting that turns it on
  (``phone_link_view.feature_hints``). Its buttons open the notification
  list, the recent calls, the calls panel and Settings.
- :class:`RecentCallsScreen` (``h``) lists the iPhone's recent calls grouped
  by day (``phone_overview.call_groups``), with call back and message.

Every blocking backend call runs off the UI thread, and iPhone text is
rendered as Rich ``Text`` after ``terminal_text``, so it carries neither
markup nor terminal controls.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, ClassVar, Protocol

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, ListItem, ListView, Static

from blueferry import phone_link_view
from blueferry import phone_overview as overview
from blueferry.i18n import _
from blueferry.models import BackendStatus, CallHistoryEntry, phone_status_fields
from blueferry.text_safety import terminal_text
from blueferry.tui_tasks import TextualTaskRunner

MAX_CALLS = 100


class CallHistoryClient(Protocol):
    def call_history(self, limit: int = 200) -> list[CallHistoryEntry]: ...

    # CallHistory1.SyncCallHistory: read the iPhone's list again.
    def sync_call_history(self) -> int: ...


def _plain(value: object) -> str:
    return terminal_text(value).replace("\n", " ")


def load_phone_name() -> str:
    """The configured iPhone's Bluetooth name (blocking; run off the UI thread)."""
    from blueferry.setup_client import SetupClient

    try:
        setup = SetupClient()
        mac = setup.configuration().mac
        devices = [device.to_dict() for device in setup.devices()]
    except Exception:
        return "iPhone"
    return phone_link_view.phone_name(devices, mac)


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


# ---- the iPhone overview ----------------------------------------------------


def feature_labels() -> dict[str, str]:
    """What each ``feature_hints`` key is called in the overview."""
    return {
        "calls": _("Phone calls"),
        "callHistory": _("Recent calls"),
        "notifications": _("Notification list"),
        "proximity": _("Away lock"),
    }


def phone_status_text(status: Mapping[str, Any]) -> str:
    """Battery, signal and network, or why they are missing."""
    fields = phone_status_fields(BackendStatus.from_dict(dict(status)))
    if not fields:
        return phone_link_view.feature_hints(status).get("phoneStatus", "")
    return "  ·  ".join(
        _("{label} {value}").format(label=label, value=value) for label, value in fields
    )


def media_text(status: Mapping[str, Any], view: overview.NowPlaying) -> Text:
    """Now playing, or the setting that turns it on, or that it is waiting."""
    hint = phone_link_view.feature_hints(status).get("media", "")
    if hint:
        return Text(_plain(hint), style="dim")
    if not view.visible:
        return Text(_("Waiting for the iPhone's media service."), style="dim")
    return now_playing_text(view)


def hints_text(status: Mapping[str, Any]) -> Text:
    """One line per opt-in feature that is off: its name and where to turn it on."""
    hints = phone_link_view.feature_hints(status)
    text = Text()
    for key, label in feature_labels().items():
        hint = hints.get(key)
        if not hint:
            continue
        if text:
            text.append("\n")
        text.append(f"{label}: ", style="bold")
        text.append(_plain(hint))
    return text


class PhoneOverviewScreen(ModalScreen[str | None]):
    """The phone card as a terminal dialog; dismisses with what to open next.

    The result is ``notifications``, ``recent_calls``, ``calls``, ``options``
    or ``None``. The app keeps it current through :meth:`status_changed`,
    :meth:`now_playing_changed` and :meth:`name_loaded`.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "Close", show=False),
    ]

    def __init__(
        self,
        status: Mapping[str, Any],
        now_playing: overview.NowPlaying,
        name: str = "",
        *,
        load_name: Callable[[], str] | None = None,
        on_name: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__()
        self._shown = False  # composed and mounted; widgets exist
        self.status: Mapping[str, Any] = dict(status)
        self.now_playing = now_playing
        self.phone_name = name or "iPhone"
        self._load_name = load_name
        self._on_name = on_name

    def compose(self) -> ComposeResult:
        with Vertical(id="phone-overview-dialog", classes="dialog"):
            yield Static(_("iPhone"), classes="dialog-title")
            yield Static("", id="phone-overview-name")
            yield Static("", id="phone-overview-connection", classes="phone-overview-line")
            yield Static("", id="phone-overview-status", classes="phone-overview-line")
            yield Static("", id="phone-overview-media", classes="phone-overview-line")
            yield Static("", id="phone-overview-hints", classes="phone-overview-hint")
            with Horizontal(classes="dialog-actions"):
                yield Button(_("Close"), id="phone-overview-close")
                yield Button(_("Settings"), id="phone-overview-options")
                yield Button(_("Notifications"), id="phone-overview-notifications")
                yield Button(_("Recent Calls"), id="phone-overview-recent")
                yield Button(_("Calls"), variant="primary", id="phone-overview-calls")

    def on_mount(self) -> None:
        self._shown = True
        self._update_view()
        self.query_one("#phone-overview-close", Button).focus()
        if self._load_name is not None:
            # A reply after the dialog closed is dropped with the dialog.
            TextualTaskRunner(self, group="phone-name").submit(
                self._load_name, self._name_read,
            )

    def status_changed(self, status: Mapping[str, Any]) -> None:
        self.status = dict(status)
        self._update_view()

    def now_playing_changed(self, view: overview.NowPlaying) -> None:
        self.now_playing = view
        self._update_view()

    def _name_read(self, value: object) -> None:
        name = str(value or "") or "iPhone"
        if self._on_name is not None:
            self._on_name(name)
        self.name_loaded(name)

    def name_loaded(self, name: str) -> None:
        self.phone_name = name or "iPhone"
        self._update_view()

    def _update_view(self) -> None:
        if not self._shown or not self.is_attached:
            return
        status = self.status
        self.query_one("#phone-overview-name", Static).update(Text(_plain(self.phone_name)))
        self.query_one("#phone-overview-connection", Static).update(
            Text(phone_link_view.connection_text(status))
        )
        self.query_one("#phone-overview-status", Static).update(
            Text(_plain(phone_status_text(status)))
        )
        self.query_one("#phone-overview-media", Static).update(
            media_text(status, self.now_playing)
        )
        hints = hints_text(status)
        widget = self.query_one("#phone-overview-hints", Static)
        widget.update(hints)
        widget.display = bool(hints)
        feature = phone_link_view.feature_hints(status)
        self.query_one("#phone-overview-recent", Button).disabled = "callHistory" in feature
        self.query_one("#phone-overview-calls", Button).disabled = "calls" in feature

    @on(Button.Pressed, "#phone-overview-notifications")
    def notifications_button(self) -> None:
        self.dismiss("notifications")

    @on(Button.Pressed, "#phone-overview-recent")
    def recent_button(self) -> None:
        self.dismiss("recent_calls")

    @on(Button.Pressed, "#phone-overview-calls")
    def calls_button(self) -> None:
        self.dismiss("calls")

    @on(Button.Pressed, "#phone-overview-options")
    def options_button(self) -> None:
        self.dismiss("options")

    @on(Button.Pressed, "#phone-overview-close")
    def close_button(self) -> None:
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)


# ---- recent calls -----------------------------------------------------------


def call_group_text(row: Mapping[str, Any]) -> Text:
    """One ``call_groups`` row: title, detail and time, plain text."""
    text = Text(_plain(row["title"]), style="bold red" if row["missed"] else "")
    text.append(f"  {_plain(row['detail'])}  ·  {_plain(row['clock'])}", style="dim")
    return text


class CallGroupItem(ListItem):
    """A recent-call row; the tooltip reads direction, caller and time."""

    def __init__(self, row: Mapping[str, Any]) -> None:
        self.row = dict(row)
        super().__init__(Static(call_group_text(row)), classes="recent-calls-row")
        # A Text, never a str: Textual parses a str tooltip as markup, and
        # the caller's name comes from the phone.
        self.tooltip = Text(_plain(
            f"{row['direction_label']}, {row['caller']}, {row['clock']}"
        ))


class DayItem(ListItem):
    def __init__(self, day: str) -> None:
        self.day = _plain(day)
        super().__init__(Static(Text(self.day)), classes="recent-calls-day", disabled=True)


RecentCallsResult = tuple[str, str] | None


class RecentCallsScreen(ModalScreen[RecentCallsResult]):
    """The iPhone's retained recent calls, newest first, grouped by day.

    Dismisses with ``("call", number)`` or ``("message", number)`` for the
    selected row, or ``None``.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape,h", "close", "Close", show=False),
        Binding("m", "toggle_missed", "Missed only", show=False),
        # Screen bindings win over the app's while this screen is open, so
        # "s" refreshes here and stars a conversation elsewhere.
        Binding("s", "sync", "Refresh from iPhone", show=False),
    ]

    def __init__(
        self, client: CallHistoryClient, *, status: Mapping[str, Any] | None = None,
        now: Callable[[], Any] | None = None,
    ) -> None:
        super().__init__()
        self._shown = False  # composed and mounted; widgets exist
        self._client = client
        self._status: Mapping[str, Any] = dict(status or {})
        self._now = now
        self.entries: list[CallHistoryEntry] = []
        self.groups: list[dict[str, Any]] = []
        self.missed_only = False
        self.error = ""
        self.loaded = False
        self.syncing = False
        self.sync_error = ""
        self._loading = False
        self._again = False
        self._runner = TextualTaskRunner(self, group="recent-calls")
        self._sync_runner = TextualTaskRunner(self, group="recent-calls-sync")

    def compose(self) -> ComposeResult:
        with Vertical(id="recent-calls-dialog", classes="dialog"):
            yield Static(_("Recent calls"), classes="dialog-title")
            yield Static("", id="recent-calls-sync-state", classes="dialog-copy")
            yield Static(_("Loading…"), id="recent-calls-state", classes="dialog-copy")
            yield ListView(id="recent-calls-list")
            with Horizontal(classes="dialog-actions"):
                yield Button(_("Missed only  m"), id="recent-calls-missed")
                yield Button(_("Refresh from iPhone  s"), id="recent-calls-sync")
                yield Button(_("Send a Message"), id="recent-calls-message")
                yield Button(_("Call Back"), id="recent-calls-call")
                yield Button(_("Close"), variant="primary", id="recent-calls-close")

    def on_mount(self) -> None:
        self._shown = True
        self.query_one("#recent-calls-list", ListView).display = False
        self._render_buttons()
        self.refresh_calls()

    def status_changed(self, status: Mapping[str, Any]) -> None:
        self._status = dict(status)
        self._render_buttons()

    @property
    def calls_hint(self) -> str:
        return phone_link_view.calls_hint(
            self._status, str(self._status.get("calls_state") or "disabled"),
        )

    def refresh_calls(self) -> None:
        if self._loading:
            self._again = True
            return
        self._loading = True
        self._runner.submit(
            lambda: self._client.call_history(MAX_CALLS),
            self._show, self._show_error, self._loaded,
        )

    def _loaded(self) -> None:
        self._loading = False
        if self._again:
            self._again = False
            self.refresh_calls()

    def _show(self, entries: object) -> None:
        self.entries = list(entries) if isinstance(entries, list) else []
        self.error, self.loaded = "", True
        self._schedule_render()

    def _show_error(self, message: str) -> None:
        self.error, self.loaded = message or _("Call history is unavailable"), True
        self._schedule_render()

    def _schedule_render(self) -> None:
        self.run_worker(self._render_calls(), group="recent-calls-render", exclusive=True)

    @property
    def body_text(self) -> str:
        """Everything the list shows, for tests and screen readers."""
        lines = []
        for item in self.query_one("#recent-calls-list", ListView).children:
            if isinstance(item, CallGroupItem):
                lines.append(call_group_text(item.row).plain)
            elif isinstance(item, DayItem):
                lines.append(item.day)
        return "\n".join(lines)

    async def _render_calls(self) -> None:
        self.query_one("#recent-calls-missed", Button).label = (
            _("All calls  m") if self.missed_only else _("Missed only  m")
        )
        state = self.query_one("#recent-calls-state", Static)
        listing = self.query_one("#recent-calls-list", ListView)
        now = self._now() if self._now is not None else None
        self.groups = (
            [] if self.error
            else overview.call_groups(self.entries, missed_only=self.missed_only, now=now)
        )
        await listing.clear()
        if self.error:
            state.update(Text(
                _("Call history is unavailable: {error}").format(error=_plain(self.error))
            ))
        elif not self.groups:
            state.update(
                _("Loading…") if not self.loaded
                else _("No missed calls") if self.missed_only else _("No recent calls")
            )
        items: list[ListItem] = []
        day = None
        for row in self.groups:
            if row["day"] != day:
                day = row["day"]
                items.append(DayItem(day))
            items.append(CallGroupItem(row))
        if items:
            await listing.extend(items)
            listing.index = 1
        state.display = not items
        listing.display = bool(items)
        self._render_buttons()

    def _selected(self) -> dict[str, Any] | None:
        listing = self.query_one("#recent-calls-list", ListView)
        item = listing.highlighted_child
        return item.row if isinstance(item, CallGroupItem) else None

    def _render_buttons(self) -> None:
        if not self._shown or not self.is_attached:
            return
        row = self._selected() if self.groups else None
        address = str(row.get("address") or "") if row else ""
        self.query_one("#recent-calls-sync", Button).disabled = self.syncing
        sync_state = self.query_one("#recent-calls-sync-state", Static)
        if self.syncing:
            sync_state.update(_("Reading the call history from the iPhone…"))
        else:
            sync_state.update(Text(_plain(self.sync_error)))
        sync_state.display = self.syncing or bool(self.sync_error)
        self.query_one("#recent-calls-message", Button).disabled = not address
        call = self.query_one("#recent-calls-call", Button)
        call.disabled = not address or bool(self.calls_hint)
        call.tooltip = Text(_plain(self.calls_hint)) if self.calls_hint else None

    @on(ListView.Highlighted, "#recent-calls-list")
    def row_highlighted(self) -> None:
        self._render_buttons()

    @on(ListView.Selected, "#recent-calls-list")
    def row_selected(self) -> None:
        self.call_back()

    @on(Button.Pressed, "#recent-calls-call")
    def call_back(self) -> None:
        row = self._selected()
        address = str(row.get("address") or "") if row else ""
        if not address:
            return
        hint = self.calls_hint
        if hint:
            self.notify(_plain(hint), severity="warning", markup=False)
            return
        self.dismiss(("call", phone_link_view.dialable(address)))

    @on(Button.Pressed, "#recent-calls-message")
    def message(self) -> None:
        row = self._selected()
        address = str(row.get("address") or "") if row else ""
        if address:
            self.dismiss(("message", address))

    @on(Button.Pressed, "#recent-calls-sync")
    def sync_button(self) -> None:
        self.action_sync()

    def action_sync(self) -> None:
        """Have the daemon read the iPhone's call history again.

        ``CallHistoryChanged`` reloads the list; a successful sync also
        reloads it here in case the list did not change.
        """
        if self.syncing:
            return
        self.syncing, self.sync_error = True, ""
        self._render_buttons()

        def failed(message: str) -> None:
            self.sync_error = _("Call history sync failed: {error}").format(
                error=message or _("unknown error"),
            )

        def finished() -> None:
            self.syncing = False
            self._render_buttons()

        self._sync_runner.submit(
            self._client.sync_call_history,
            lambda _count: self.refresh_calls(), failed, finished,
        )

    @on(Button.Pressed, "#recent-calls-missed")
    def missed_button(self) -> None:
        self.action_toggle_missed()

    @on(Button.Pressed, "#recent-calls-close")
    def close_button(self) -> None:
        self.dismiss(None)

    def action_toggle_missed(self) -> None:
        self.missed_only = not self.missed_only
        self._schedule_render()

    def action_close(self) -> None:
        self.dismiss(None)
