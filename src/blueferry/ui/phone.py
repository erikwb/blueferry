"""iPhone dialog: phone status, opt-in now playing and recent calls.

Every row sets ``use_markup=False``: titles and subtitles carry iPhone text
(track titles, caller names) verbatim and must never be parsed as Pango
markup. Now Playing and Recent Calls appear only while their opt-ins are on
(switched in iPhone Settings), and nothing is fetched while the dialog is
closed.
"""

from __future__ import annotations

from gi.repository import Adw, Gtk

from blueferry import phone_overview as overview
from blueferry.i18n import _
from blueferry.models import BackendStatus, CallHistoryEntry
from blueferry.ui.phone_presenter import media, status_rows

MAX_CALLS = 100


def _media_buttons() -> dict[str, tuple[str, str]]:
    return {
        "previous": ("media-skip-backward-symbolic", _("Previous Track")),
        "toggle": ("media-playback-start-symbolic", _("Play or Pause")),
        "next": ("media-skip-forward-symbolic", _("Next Track")),
    }


def _row(title: str = "", subtitle: str = "") -> Adw.ActionRow:
    return Adw.ActionRow(
        title=title, subtitle=subtitle, subtitle_lines=0, use_markup=False,
    )


class PhonePage(Gtk.Box):
    def __init__(self, client, toast) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._client = client
        self._toast = toast
        self._status: dict = BackendStatus().to_dict()
        self._now_playing: dict = {}
        self._calls: list[CallHistoryEntry] = []
        self._calls_error = ""
        self._calls_received = False
        self._watching = False
        self._epoch = 0
        self._pending: set[str] = set()
        self._again: set[str] = set()

        page = Adw.PreferencesPage()
        page.set_vexpand(True)
        self.append(page)

        self._status_group = Adw.PreferencesGroup(title=_("iPhone"))
        page.add(self._status_group)
        self._status_rows: list[Adw.ActionRow] = []

        self._media_group = Adw.PreferencesGroup(title=_("Now Playing"))
        page.add(self._media_group)
        self._media_row = _row()
        self._media_buttons: dict[str, Gtk.Button] = {}
        for command, (icon, label) in _media_buttons().items():
            button = Gtk.Button(
                icon_name=icon, valign=Gtk.Align.CENTER, tooltip_text=label,
                css_classes=["flat"],
            )
            button.update_property([Gtk.AccessibleProperty.LABEL], [label])
            button.connect("clicked", lambda _b, name=command: self._media_command(name))
            self._media_row.add_suffix(button)
            self._media_buttons[command] = button
        self._media_group.add(self._media_row)

        self._calls_group = Adw.PreferencesGroup(title=_("Recent Calls"))
        self._missed_only = Gtk.ToggleButton(
            label=_("Missed Only"), valign=Gtk.Align.CENTER, css_classes=["flat"],
        )
        self._missed_only.connect("toggled", lambda _b: self._render_calls())
        self._calls_group.set_header_suffix(self._missed_only)
        page.add(self._calls_group)
        self._call_rows: list[Adw.ActionRow] = []

        client.connect("status-invalidated", lambda _c: self._refresh_status())
        client.connect("now-playing-changed", lambda _c: self._refresh_media())
        client.connect("call-history-changed", lambda _c: self._refresh_calls())
        self._render()

    # ---- loading --------------------------------------------------------

    def set_watching(self, watching: bool) -> None:
        """Fetch only while the dialog is open, like the Qt Recent Calls page."""
        if self._watching != watching:
            self._epoch += 1
        self._watching = watching
        if watching:
            self._refresh_status()
        else:
            self._again.clear()
            self._status_loaded(BackendStatus())

    def _load(self, kind, request, loaded, failed) -> None:
        if kind in self._pending:
            self._again.add(kind)
            return
        self._pending.add(kind)
        epoch = self._epoch

        def completed(callback, value):
            self._pending.discard(kind)
            again = kind in self._again
            self._again.discard(kind)
            if self._watching and epoch == self._epoch:
                callback(value)
            if again and self._watching:
                {"status": self._refresh_status, "media": self._refresh_media,
                 "calls": self._refresh_calls}[kind]()

        request(lambda value: completed(loaded, value), lambda error: completed(failed, error))

    def _refresh_status(self) -> None:
        if self._watching:
            self._load("status", self._client.get_status_async, self._status_loaded,
                       lambda _message: self._status_loaded(BackendStatus()))

    def _refresh_media(self) -> None:
        if not self._watching or not overview.media_control_enabled(self._status):
            return
        self._load(
            "media", self._client.now_playing_async,
            self._media_loaded, lambda _message: self._media_loaded({}),
        )

    def _refresh_calls(self) -> None:
        if not self._watching or not overview.call_history_enabled(self._status):
            return
        self._load(
            "calls",
            lambda loaded, failed: self._client.call_history_async(MAX_CALLS, loaded, failed),
            self._calls_loaded, self._calls_failed,
        )

    def _status_loaded(self, status: BackendStatus) -> None:
        values = status.to_dict()
        if any(self._status.get(key) != values.get(key) for key in (
            "daemon", "media_control_enabled", "call_history_enabled",
        )):
            self._epoch += 1
        self._status = values
        if not overview.media_control_enabled(self._status):
            self._now_playing = {}
        if not overview.call_history_enabled(self._status):
            self._calls, self._calls_error = [], ""
            self._calls_received = False
        self._render()
        self._refresh_media()
        self._refresh_calls()

    def _media_loaded(self, snapshot: object) -> None:
        self._now_playing = snapshot if isinstance(snapshot, dict) else {}
        self._render_media()

    def _calls_loaded(self, entries: list[CallHistoryEntry]) -> None:
        self._calls, self._calls_error = list(entries), ""
        self._calls_received = True
        self._render_calls()

    def _calls_failed(self, message: str) -> None:
        self._calls_error = message or _("Call history is unavailable")
        self._calls_received = True
        self._render_calls()

    # ---- rendering ------------------------------------------------------

    def _render(self) -> None:
        for row in self._status_rows:
            self._status_group.remove(row)
        self._status_rows = []
        for label, value in status_rows(self._status):
            row = _row(label, value)
            self._status_group.add(row)
            self._status_rows.append(row)
        self._render_media()
        self._render_calls()

    def _render_media(self) -> None:
        state = media(self._status, self._now_playing)
        self._media_group.set_visible(state.shown)
        if not state.shown:
            return
        self._media_row.set_title(state.title)
        self._media_row.set_subtitle(state.subtitle)
        for name, button in self._media_buttons.items():
            button.set_sensitive(bool(state.buttons and state.buttons.get(name)))
        self._media_buttons["toggle"].set_icon_name(
            "media-playback-pause-symbolic" if state.playing
            else "media-playback-start-symbolic"
        )

    def _render_calls(self) -> None:
        shown = overview.call_history_enabled(self._status)
        self._calls_group.set_visible(shown)
        for row in self._call_rows:
            self._calls_group.remove(row)
        self._call_rows = []
        if not shown:
            return
        missed_only = self._missed_only.get_active()
        rows = overview.call_rows(self._calls[:MAX_CALLS], missed_only=missed_only)
        if self._calls_error:
            placeholder = [_row(_("Call history is unavailable"), self._calls_error)]
        elif not self._calls_received:
            placeholder = [_row(_("Loading…"))]
        elif not rows:
            placeholder = [_row(_("No missed calls") if missed_only else _("No recent calls"))]
        else:
            placeholder = []
        for row in placeholder:
            self._calls_group.add(row)
            self._call_rows.append(row)
        for item in rows:
            row = _row(item.caller, item.detail)
            if item.missed:
                row.add_css_class("error")
            time = Gtk.Label(label=item.time, use_markup=False, css_classes=["dim-label"])
            row.add_suffix(time)
            self._calls_group.add(row)
            self._call_rows.append(row)

    # ---- actions --------------------------------------------------------

    def _media_command(self, command: str) -> None:
        def failed(message: str) -> None:
            self._toast(message)
            self._refresh_media()

        self._client.send_media_command_async(command, lambda _value: None, failed)
