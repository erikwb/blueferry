"""Settings > Notifications: mute the desktop popups of single iPhone apps.

The GTK counterpart of the Qt client's muted-apps section, on the shared
:mod:`blueferry.notification_apps_view` rows and texts. The group is shown
only for daemons that report ``notification_muted_app_count``. The list is
read when the settings open, when that count changes and on
``NotificationAppsChanged``, the last two only while the dialog is shown.
Every D-Bus call runs on the client's workers. App names come from the
iPhone and are shown as plain text.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from gi.repository import Adw, Gtk

from blueferry import notification_apps_view as view
from blueferry.models import BackendStatus

_UNREAD = object()


class MutedApps:
    def __init__(
        self, client, toast: Callable[[str], None], *, is_open: Callable[[], bool],
    ) -> None:
        self._client = client
        self._toast = toast
        self._is_open = is_open
        self._shown = False
        self._reachable = False
        self._count: object = _UNREAD
        self._loaded_count: object = _UNREAD
        self._payload: dict[str, Any] = view.normalize({})
        self._saving: set[str] = set()
        self._applying = False
        self.rows: dict[str, Adw.SwitchRow] = {}
        self.group = Adw.PreferencesGroup(
            title=view.section_label(), description=view.section_description(),
        )
        self.group.set_visible(False)
        # Not a row: AdwPreferencesGroup shows it below all the rows.
        self.hint = Gtk.Label(
            xalign=0, wrap=True, use_markup=False, margin_top=6,
            css_classes=["dim-label", "caption"],
        )
        self.group.add(self.hint)
        self._render()
        client.connect("notification-apps-changed", lambda *_args: self._changed())

    def apply(self, status: BackendStatus, reachable: bool) -> None:
        values = status.to_dict()
        shown = reachable and view.supported(values)
        changed = (shown, reachable) != (self._shown, self._reachable)
        self._reachable = reachable
        self._shown = shown
        self.group.set_visible(shown)
        self._count = values.get(view.STATUS_KEY, _UNREAD)
        # A mute from the CLI or another client changes the count; the
        # first read happens when the settings open (load).
        if shown and self._is_open() and self._count != self._loaded_count:
            self.load()
        # Only reachability changes the rows; the list itself comes from load.
        if changed:
            self._render()

    def load(self) -> None:
        if self._shown:
            self._loaded_count = self._count
            self._client.notification_apps_async(self._loaded, lambda _message: None)

    def _changed(self) -> None:
        if self._is_open():
            self.load()

    def _loaded(self, payload: object) -> None:
        self._payload = view.normalize(payload)
        self._render()

    def _render(self) -> None:
        rows = view.app_rows(self._payload)
        wanted = [row["app_id"] for row in rows]
        for app_id in [key for key in self.rows if key not in wanted]:
            self.group.remove(self.rows.pop(app_id))
        self._applying = True
        try:
            for row in rows:
                widget = self.rows.get(row["app_id"])
                if widget is None:
                    widget = Adw.SwitchRow(use_markup=False)
                    widget.connect(
                        "notify::active",
                        lambda switch, _p, app_id=row["app_id"]: self._toggled(app_id, switch),
                    )
                    self.rows[row["app_id"]] = widget
                    self.group.add(widget)
                widget.set_title(row["name"])
                widget.set_subtitle(row["subtitle"])
                widget.set_tooltip_text(row["toggle_label"])
                if row["app_id"] not in self._saving:
                    widget.set_active(row["muted"])
                widget.set_sensitive(
                    self._reachable and row["enabled"] and row["app_id"] not in self._saving
                )
        finally:
            self._applying = False
        hint = view.apps_hint(self._payload)
        self.hint.set_label(hint)
        self.hint.set_visible(bool(hint))
        # Keep the hint below the rows.
        self.group.remove(self.hint)
        self.group.add(self.hint)

    def _toggled(self, app_id: str, switch: Adw.SwitchRow) -> None:
        if self._applying or app_id in self._saving:
            return
        muted = switch.get_active()
        current = next(
            (row for row in view.app_rows(self._payload) if row["app_id"] == app_id), None,
        )
        if current is None or not current["enabled"] or current["muted"] == muted:
            self._render()
            return
        self._saving.add(app_id)

        def finished() -> None:
            self._saving.discard(app_id)
            self.load()
            self._render()

        def saved(state: object) -> None:
            # Show the confirmed state now instead of the old list until the
            # read below answers.
            new_state = state if isinstance(state, bool) else muted
            self._payload = view.normalize({**self._payload, "apps": [
                {**app, "muted": new_state} if app["app_id"] == app_id else app
                for app in self._payload["apps"]
            ]})
            finished()

        def failed(message: str) -> None:
            self._toast(view.change_failed_text(message))
            finished()

        self._client.set_notification_app_muted_async(app_id, muted, saved, failed)
        self._render()
