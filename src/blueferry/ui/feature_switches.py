"""The ``local.env`` feature switches and "Restart Service" in GTK settings.

The state and wording come from :mod:`blueferry.features_view`, shared with
the Qt client. Switches load through ``Messages1.GetFeatures`` and change
through ``SetFeature``; the daemon reads them at start-up, so a change asks
for a service restart. Older daemons without ``GetFeatures`` show each
switch insensitive with the ``local.env`` variable to set instead.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from gi.repository import Adw, Gtk

from blueferry import features_view
from blueferry.client_tasks import TimerFactory
from blueferry.i18n import _
from blueferry.ui.settings_presenter import feature_subtitle
from blueferry.ui.util import glib_timer


class FeatureSwitches:
    """Owns the switch rows and the restart notices of every settings page."""

    def __init__(
        self,
        client,
        toast: Callable[[str], None],
        refresh_status: Callable[[], None],
        timer: TimerFactory | None = None,
        is_open: Callable[[], bool] = lambda: True,
    ) -> None:
        self._client = client
        self._toast = toast
        self._is_open = is_open
        self._state: dict[str, Any] = features_view.initial()
        self._rows: dict[str, tuple[Adw.SwitchRow, str]] = {}
        self._notices: list[tuple[Adw.PreferencesGroup, Adw.ActionRow]] = []
        self._restart_buttons: list[Gtk.Button] = []
        self._saving: set[str] = set()
        self._applying = False
        self._daemon = False
        self._restarting = False
        make_timer = timer or glib_timer
        # After a restart: the status first, then the switches, once the
        # restarted daemon is likely to answer (as in the Qt client).
        self._status_timer = make_timer(refresh_status)
        self._features_timer = make_timer(self.load)

    @property
    def state(self) -> dict[str, Any]:
        return features_view.view(self._state)

    def row(self, name: str, title: str, description: str = "") -> Adw.SwitchRow:
        row = Adw.SwitchRow(title=title, use_markup=False)
        row.connect("notify::active", self._toggled, name)
        self._rows[name] = (row, description)
        self._render_row(name)
        return row

    def notice_group(self) -> Adw.PreferencesGroup:
        """A "restart required" notice for the top of one settings page."""
        group = Adw.PreferencesGroup()
        row = Adw.ActionRow(title=features_view.restart_notice(self._state), use_markup=False)
        row.add_prefix(Gtk.Image(icon_name="system-reboot-symbolic"))
        row.add_suffix(self.restart_button(suggested=True))
        group.add(row)
        self._notices.append((group, row))
        self._render_notices()
        return group

    def restart_button(self, *, suggested: bool = False) -> Gtk.Button:
        """A "Restart Service" button, insensitive while a restart runs."""
        button = Gtk.Button(label=_("Restart Service"), valign=Gtk.Align.CENTER)
        if suggested:
            button.add_css_class("suggested-action")
        button.connect("clicked", lambda _button: self.restart())
        self._restart_buttons.append(button)
        button.set_sensitive(not self._restarting)
        return button

    def set_daemon(self, reachable: bool) -> None:
        if reachable != self._daemon:
            self._daemon = reachable
            self._render()

    def load(self) -> None:
        def loaded(value: object) -> None:
            self._state = features_view.loaded(self._state, value)
            self._render()

        def failed(message: str) -> None:
            # Older daemons have no GetFeatures; the rows fall back to hints.
            self._state = features_view.unavailable(self._state, message)
            self._render()

        self._client.features_async(loaded, failed)

    def restart(self) -> None:
        if self._restarting:
            return
        self._restarting = True
        self._render_notices()

        def restarted(_value: object) -> None:
            self._restarting = False
            self._state = features_view.with_notice(self._state, "")
            self._render_notices()
            # A restart that ends after Settings closed reloads on the next
            # visit (open() loads the switches) instead of in the background.
            if self._is_open():
                self._status_timer.start(features_view.RESTART_REFRESH_DELAY_MS)
                self._features_timer.start(features_view.RESTART_FEATURES_DELAY_MS)

        def failed(message: str) -> None:
            self._restarting = False
            self._render_notices()
            self._toast(_("Could not restart the service: {error}").format(error=message))

        self._client.restart_backend_async(restarted, failed)

    @property
    def restarting(self) -> bool:
        return self._restarting

    def stop(self) -> None:
        """Cancel the pending reloads, e.g. when the settings dialog closes."""
        self._status_timer.stop()
        self._features_timer.stop()

    def _toggled(self, row: Adw.SwitchRow, _pspec, name: str) -> None:
        if self._applying:
            return
        enabled = row.get_active()
        # Show the choice while it is saved; a failure puts the old state back.
        previous = self._state
        self._state = features_view.assume(previous, name, enabled)
        self._saving.add(name)
        self._render()

        def saved(value: object) -> None:
            self._saving.discard(name)
            self._state = features_view.with_notice(
                self._state, features_view.set_notice(value)
            )
            self._render()
            self.load()

        def failed(message: str) -> None:
            self._saving.discard(name)
            self._state = previous
            self._toast(_("Could not save the setting: {error}").format(error=message))
            self._render()

        self._client.set_feature_async(name, enabled, saved, failed)

    def _render(self) -> None:
        for name in self._rows:
            self._render_row(name)
        self._render_notices()

    def _render_row(self, name: str) -> None:
        row, description = self._rows[name]
        switch = features_view.switch(self._state, name)
        saving = name in self._saving
        if not saving:
            # Keep the user's position while the change is being saved.
            self._applying = True
            row.set_active(switch["active"])
            self._applying = False
        row.set_subtitle(feature_subtitle(description, switch["note"]))
        row.set_sensitive(switch["editable"] and self._daemon and not saving)

    def _render_notices(self) -> None:
        pending = features_view.restart_pending(self._state)
        notice = features_view.restart_notice(self._state)
        for group, row in self._notices:
            group.set_visible(pending)
            row.set_title(notice)
        for button in self._restart_buttons:
            button.set_sensitive(not self._restarting)
