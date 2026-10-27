"""The Notifications tab: recent iPhone app notifications (opt-in).

GTK counterpart of the Qt client's ``NotificationsTab.qml``, on the shared
:class:`blueferry.notification_history.NotificationHistory` model. The list
is private and kept only in the daemon's memory: it is read while the tab
is shown (:meth:`NotificationsPage.set_watched`) and forgotten, with its
pending reload, as soon as the tab or the window is hidden. Every field
comes from the iPhone and is shown as plain text.
"""

from __future__ import annotations

from collections.abc import Callable

from gi.repository import Adw, GLib, Gtk, Pango

from blueferry import phone_link_view
from blueferry.client_tasks import TimerFactory
from blueferry.i18n import _
from blueferry.notification_history import NotificationHistory
from blueferry.ui.util import glib_timer


class NotificationsPage(Gtk.Box):
    def __init__(
        self,
        client,
        *,
        open_settings: Callable[[str], None],
        timer: TimerFactory | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        self._opt_in_hint = phone_link_view.feature_hints({}).get("notifications", "")
        self.history = NotificationHistory(
            runner=client, timer=timer or glib_timer,
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
        self._render()

    def apply_status(self, status: dict) -> None:
        """A status read by the phone card: is the list switched on?"""
        hint = phone_link_view.feature_hints(status).get("notifications", "")
        if hint != self._opt_in_hint:
            self._opt_in_hint = hint
            self.history.refresh()
            self._render()

    def set_watched(self, watched: bool) -> None:
        if watched != self.history.watched:
            self.history.watch(watched)
            self._render()

    def _render(self) -> None:
        rows = self.history.rows
        view = phone_link_view.notifications_view(self.history.info, self._opt_in_hint)
        self.content_banner.set_title(view["contentHint"])
        self.content_banner.set_revealed(bool(view["contentHint"]))
        self.refresh_button.set_sensitive(not self._opt_in_hint)
        self.list.remove_all()
        for item in rows:
            self.list.append(self._row(item))
        self.placeholder.set_title(view["title"])
        self.placeholder.set_description(GLib.markup_escape_text(view["explanation"]))
        self.settings_button.set_visible(bool(self._opt_in_hint))
        self.stack.set_visible_child_name("list" if rows else "placeholder")

    def _row(self, item: dict) -> Adw.ActionRow:
        subtitle = "\n".join(part for part in (item["summary"], item["body"]) if part)
        row = Adw.ActionRow(
            title=item["app"], subtitle=subtitle, subtitle_lines=4, use_markup=False,
        )
        row.update_property(
            [Gtk.AccessibleProperty.LABEL], [f"{item['app']}, {item['time']}"],
        )
        row.add_prefix(Gtk.Image(icon_name="preferences-system-notifications-symbolic"))
        row.add_suffix(Gtk.Label(
            label=item["time"], css_classes=["dim-label", "numeric"],
            ellipsize=Pango.EllipsizeMode.END, valign=Gtk.Align.START,
        ))
        return row
