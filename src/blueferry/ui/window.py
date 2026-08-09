"""Main application window."""
from __future__ import annotations

from gi.repository import Adw, Gio, Gtk

from blueferry.i18n import _
from blueferry.setup_client import SetupClient
from blueferry.ui.conversations import ConversationsPage
from blueferry.ui.status import IPhonePage


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, application, client) -> None:
        super().__init__(application=application, title=_("BlueFerry"))
        self._client = client
        self.set_default_size(940, 660)

        self._toasts = Adw.ToastOverlay()
        self._stack = Adw.ViewStack(vexpand=True, hexpand=True)

        self.messages = ConversationsPage(client, self.toast)
        self.iphone = IPhonePage(client, self.toast)
        for widget, name, title, icon in (
            (self.messages, "conversations",
             _("Messages"), "mail-unread-symbolic"),
            (self.iphone, "iphone", _("iPhone"), "phone-symbolic"),
        ):
            self._stack.add_titled_with_icon(widget, name, title, icon)
        self._configured = SetupClient().configuration().configured
        if not self._configured:
            self._stack.set_visible_child_name("iphone")

        self._switcher = Adw.ViewSwitcher(
            stack=self._stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header = Adw.HeaderBar(title_widget=self._switcher)

        menu = Gio.Menu()
        menu.append(_("Keyboard Shortcuts"), "app.shortcuts")
        menu.append(_("About BlueFerry"), "app.about")
        menu_button = Gtk.MenuButton(
            icon_name="open-menu-symbolic",
            menu_model=menu,
            tooltip_text=_("Main Menu"),
        )
        menu_button.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Main Menu")]
        )
        header.pack_end(menu_button)

        self._banner = Adw.Banner(
            title=_("Background service unavailable — open iPhone to repair it"),
            button_label=_("Open iPhone"),
        )
        self._banner.connect(
            "button-clicked",
            lambda _banner: self._stack.set_visible_child_name("iphone"),
        )

        self._switcher_bar = Adw.ViewSwitcherBar(
            stack=self._stack,
            reveal=False,
        )

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(self._banner)
        content.append(self._stack)
        content.append(self._switcher_bar)
        self._toasts.set_child(content)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(self._toasts)
        self.set_content(toolbar)

        compact = Adw.Breakpoint.new(
            Adw.BreakpointCondition.parse("max-width: 650px")
        )
        compact.add_setter(self._switcher, "visible", False)
        compact.add_setter(self._switcher_bar, "reveal", True)
        compact.add_setter(self.messages.split_view, "collapsed", True)
        compact.add_setter(self.messages.back_button, "visible", True)
        self.add_breakpoint(compact)

        client.connect("availability-changed", self._on_availability)
        self._on_availability(client, client.available)

    def toast(self, text: str) -> None:
        self._toasts.add_toast(Adw.Toast(title=text))

    def _on_availability(self, _client, available: bool) -> None:
        if available:
            self._configured = True
        self._banner.set_revealed(self._configured and not available)
