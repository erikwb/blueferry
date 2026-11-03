"""The Notifications tab: recent iPhone app notifications (opt-in).

GTK counterpart of the Qt client's ``NotificationsTab.qml``, on the shared
:class:`blueferry.notification_history.NotificationHistory` model. The list
is private and kept only in the daemon's memory: it is read while the tab
is shown (:meth:`NotificationsPage.set_watched`) and forgotten, with its
pending reload, as soon as the tab or the window is hidden. Every field
comes from the iPhone and is shown as plain text.

Each row can be pinned (``notification_pin_view``): pinned rows come first
and are marked, a notification the iPhone has removed stays dimmed with
"Removed on iPhone". Rows of apps muted in Settings > Notifications are
dimmed too (``notification_apps_view.muted_ids``, read only while the tab is
shown and the daemon supports muting).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from gi.repository import Adw, GLib, Gtk, Pango

from blueferry import notification_apps_view, notification_pin_view, phone_link_view
from blueferry.client_tasks import TimerFactory
from blueferry.i18n import _
from blueferry.notification_history import NotificationHistory
from blueferry.ui.util import glib_timer


class _SnapshotRunner:
    """The client's TaskRunner, keeping each ListNotifications answer the
    shared model accepts.

    The model keeps only display rows; the pin keys and flags stay with
    the raw records, which arrive here in the same order. The model
    re-renders (and so reads the records) while it takes an answer, so the
    answer is stored before the model sees it; an answer the model drops
    (the tab was hidden meanwhile, or a newer read superseded it) leaves its
    rows untouched, and then the records it belongs to are dropped too.
    Accepting an answer or an error always gives the model a new row list.
    """

    def __init__(
        self,
        client,
        *,
        rows: Callable[[], object],
        get: Callable[[], dict[str, Any]],
        put: Callable[[dict[str, Any]], None],
    ) -> None:
        self._client = client
        self._rows = rows
        self._get = get
        self._put = put

    def submit(self, operation, on_done=None, on_error=None, on_finished=None) -> None:
        def done(value: object) -> None:
            self._settle(value, lambda: on_done(value) if on_done is not None else None)

        def failed(message: str) -> None:
            self._settle(None, lambda: on_error(message) if on_error is not None else None)

        self._client.submit(operation, done, failed, on_finished)

    def _settle(self, value: object, deliver: Callable[[], None]) -> None:
        previous, rows = self._get(), self._rows()
        self._put(value if isinstance(value, dict) else {})
        deliver()
        if self._rows() is rows:
            self._put(previous)


class NotificationsPage(Gtk.Box):
    def __init__(
        self,
        client,
        *,
        open_settings: Callable[[str], None],
        timer: TimerFactory | None = None,
        toast: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        self._client = client
        self._toast = toast or (lambda _text: None)
        self._opt_in_hint = phone_link_view.feature_hints({}).get("notifications", "")
        # The last ListNotifications answer (records with pin keys), only
        # while the tab is shown.
        self._snapshot: dict[str, Any] = {}
        self._pinning: set[str] = set()
        self._apps_supported = False
        self._muted: frozenset[str] = frozenset()
        self._apps_serial = 0
        self.history = NotificationHistory(
            runner=_SnapshotRunner(
                client, rows=lambda: self.history.rows,
                get=lambda: self._snapshot, put=self._captured,
            ),
            timer=timer or glib_timer,
            fetch=client.notifications, on_change=self._render,
        )

        self.content_banner = Adw.Banner(use_markup=False, button_label=_("Open Settings"))
        self.content_banner.connect("button-clicked", lambda _b: open_settings("notifications"))
        self.append(self.content_banner)

        header = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
            margin_top=12, margin_bottom=6, margin_start=18, margin_end=12,
        )
        header.append(Gtk.Label(
            label=_("Notifications"), xalign=0, hexpand=True, css_classes=["heading"],
        ))
        self.refresh_button = Gtk.Button(
            icon_name="view-refresh-symbolic", tooltip_text=_("Refresh"),
            css_classes=["flat"],
        )
        self.refresh_button.update_property([Gtk.AccessibleProperty.LABEL], [_("Refresh")])
        self.refresh_button.connect("clicked", lambda _b: self.history.refresh())
        header.append(self.refresh_button)
        self.append(header)

        self.stack = Gtk.Stack(vexpand=True)
        self.list = Gtk.ListBox(
            css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE,
            valign=Gtk.Align.START, margin_top=6, margin_bottom=18,
            margin_start=12, margin_end=12,
        )
        self.stack.add_named(Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True,
            child=Adw.Clamp(maximum_size=720, child=self.list),
        ), "list")
        self.placeholder = Adw.StatusPage(
            icon_name="preferences-system-notifications-symbolic", vexpand=True,
        )
        self.settings_button = Gtk.Button(
            label=_("Open Notification Settings"), halign=Gtk.Align.CENTER,
            css_classes=["pill"],
        )
        self.settings_button.connect("clicked", lambda _b: open_settings("notifications"))
        self.placeholder.set_child(self.settings_button)
        self.stack.add_named(self.placeholder, "placeholder")
        self.append(self.stack)

        client.connect("notifications-changed", lambda _c: self.history.invalidated())
        client.connect("notification-apps-changed", lambda _c: self._load_apps())
        self._render()

    def apply_status(self, status: dict) -> None:
        """A status read by the phone card: is the list switched on?"""
        # Access, privacy and phone profile gate every read and every row.
        self.history.sync_status(status)
        hint = phone_link_view.feature_hints(status).get("notifications", "")
        if hint != self._opt_in_hint:
            self._opt_in_hint = hint
            # sync_status above already reread the list if access changed.
            self._render()
        supported = notification_apps_view.supported(status)
        if supported != self._apps_supported:
            self._apps_supported = supported
            self._load_apps()

    def set_watched(self, watched: bool) -> None:
        if watched != self.history.watched:
            self.history.watch(watched)
            if not watched:
                self._snapshot = {}
            self._load_apps()
            self._render()

    # ---- loading --------------------------------------------------------

    def _captured(self, value: dict[str, Any]) -> None:
        self._snapshot = value if self.history.watched else {}

    def _load_apps(self) -> None:
        """Read the muted apps while the tab is shown; forget them otherwise."""
        self._apps_serial += 1
        if not (self.history.watched and self._apps_supported):
            if self._muted:
                self._muted = frozenset()
                self._render()
            return
        serial = self._apps_serial

        def loaded(payload: object) -> None:
            if serial == self._apps_serial:
                self._muted = notification_apps_view.muted_ids(payload)
                self._render()

        def failed(_message: str) -> None:
            if serial == self._apps_serial and self._muted:
                self._muted = frozenset()
                self._render()

        self._client.notification_apps_async(loaded, failed)

    def _records(self) -> list[Mapping[str, Any]]:
        """The raw records behind :attr:`NotificationHistory.rows`, in order."""
        records = self._snapshot.get("notifications")
        found = [r for r in records if isinstance(r, Mapping)] if isinstance(
            records, list) else []
        return found if len(found) == len(self.history.rows) else []

    # ---- rendering ------------------------------------------------------

    def _render(self) -> None:
        rows = self.history.rows
        if not rows:
            # Cleared for access, content or profile: the records go too.
            self._snapshot = {}
        view = phone_link_view.notifications_view(self.history.info, self._opt_in_hint)
        self.content_banner.set_title(view["contentHint"])
        self.content_banner.set_revealed(bool(view["contentHint"]))
        self.refresh_button.set_sensitive(not self._opt_in_hint)
        self.list.remove_all()
        records = self._records()
        full = notification_pin_view.pin_limit_reached(self._snapshot)
        limit_text = notification_pin_view.pin_limit_text(self._snapshot.get("pins_max"))
        for index, item in enumerate(rows):
            record = records[index] if records else {}
            self.list.append(self._row(item, record, full=full, limit_text=limit_text))
        self.placeholder.set_title(view["title"])
        self.placeholder.set_description(GLib.markup_escape_text(view["explanation"]))
        self.settings_button.set_visible(bool(self._opt_in_hint))
        self.stack.set_visible_child_name("list" if rows else "placeholder")

    def _row(
        self, item: dict, record: Mapping[str, Any], *, full: bool, limit_text: str,
    ) -> Adw.ActionRow:
        pin = notification_pin_view.pin_row_extras(record)
        app_id = record.get("app_id")
        muted = isinstance(app_id, str) and app_id in self._muted
        lines = [item["summary"], item["body"], pin["removed_text"]]
        subtitle = "\n".join(part for part in lines if part)
        row = Adw.ActionRow(
            title=item["app"], subtitle=subtitle, subtitle_lines=5, use_markup=False,
        )
        states = [part for part in (
            pin["pinned_text"], pin["removed_text"],
            notification_apps_view.muted_label() if muted else "",
        ) if part]
        row.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [", ".join([item["app"], item["time"], *states])],
        )
        row.add_prefix(Gtk.Image(
            icon_name="view-pin-symbolic" if pin["pinned"]
            else "preferences-system-notifications-symbolic",
            tooltip_text=pin["pinned_text"] or None,
        ))
        if pin["removed"] or muted:
            row.add_css_class("dim-label")
        row.add_suffix(Gtk.Label(
            label=item["time"], css_classes=["dim-label", "numeric"],
            ellipsize=Pango.EllipsizeMode.END, valign=Gtk.Align.START,
        ))
        if pin["can_pin"]:
            row.add_suffix(self._pin_button(pin, full=full, limit_text=limit_text))
        return row

    def _pin_button(self, pin: dict, *, full: bool, limit_text: str) -> Gtk.Button:
        key = str(pin["key"])
        blocked = full and not pin["pinned"]
        button = Gtk.Button(
            icon_name="view-pin-symbolic", valign=Gtk.Align.CENTER,
            tooltip_text=limit_text if blocked else pin["pin_tooltip"],
            css_classes=["flat", "circular", *(["accent"] if pin["pinned"] else [])],
            sensitive=not blocked and key not in self._pinning,
        )
        button.update_property([Gtk.AccessibleProperty.LABEL], [pin["pin_label"]])
        button.connect("clicked", lambda _b: self._set_pinned(key, not pin["pinned"]))
        return button

    # ---- actions --------------------------------------------------------

    def _set_pinned(self, key: str, pinned: bool) -> None:
        if key in self._pinning:
            return
        self._pinning.add(key)

        def finished() -> None:
            self._pinning.discard(key)
            # The daemon also says NotificationsChanged; read once more anyway.
            self.history.invalidated()
            self._render()

        def failed(message: str) -> None:
            self._toast(message or notification_pin_view.GENERIC_ERROR_TEXT)
            finished()

        self._client.set_notification_pinned_async(
            key, pinned, lambda _state: finished(), failed,
        )
        self._render()
