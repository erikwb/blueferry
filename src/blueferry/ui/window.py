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
        self.messages = ConversationsPage(client, self.toast)
        self._configured = SetupClient().configuration().configured
        self._initial_setup_pending = not self._configured

        header = Adw.HeaderBar(
            title_widget=Adw.WindowTitle(
                title=_("BlueFerry"),
                subtitle=_("Messages"),
            )
        )

        menu = Gio.Menu()
        menu.append(_("iPhone Settings"), "win.phone")
        menu.append(_("Keyboard Shortcuts"), "app.shortcuts")
        menu.append(_("About BlueFerry"), "app.about")
        menu_button = Gtk.MenuButton(
            icon_name="open-menu-symbolic",
            menu_model=menu,
            tooltip_text=_("Main Menu"),
        )
        menu_button.update_property([Gtk.AccessibleProperty.LABEL], [_("Main Menu")])
        header.pack_end(menu_button)

        phone_action = Gio.SimpleAction.new("phone", None)
        phone_action.connect("activate", lambda *_args: self.present_phone_settings())
        self.add_action(phone_action)

        self._phone_toasts = Adw.ToastOverlay()
        self.iphone = IPhonePage(client, self.phone_toast)
        self._phone_toasts.set_child(self.iphone)
        phone_toolbar = Adw.ToolbarView()
        phone_toolbar.add_top_bar(
            Adw.HeaderBar(title_widget=Adw.WindowTitle(title=_("iPhone Settings")))
        )
        phone_toolbar.set_content(self._phone_toasts)
        self._phone_dialog = Adw.Dialog(
            title=_("iPhone Settings"),
            content_width=680,
            content_height=620,
            child=phone_toolbar,
        )

        self._banner = Adw.Banner(
            title=_("Background service unavailable — open iPhone to repair it"),
            button_label=_("Open iPhone"),
        )
        self._banner.connect(
            "button-clicked",
            lambda _banner: self.present_phone_settings(),
        )

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(self._banner)
        content.append(self.messages)
        self._toasts.set_child(content)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(self._toasts)
        self.set_content(toolbar)

        compact = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 650px"))
        compact.add_setter(self.messages.split_view, "collapsed", True)
        compact.add_setter(self.messages.back_button, "visible", True)
        self.add_breakpoint(compact)

        client.connect("availability-changed", self._on_availability)
        client.connect("open-message-requested", self._on_open_message_requested)
        self._on_availability(client, client.available)

    def toast(self, text: str) -> None:
        self._toasts.add_toast(Adw.Toast(title=text))

    def phone_toast(self, text: str) -> None:
        self._phone_toasts.add_toast(Adw.Toast(title=text))

    def present_phone_settings(self) -> None:
        self._phone_dialog.present(self)

    def present_initial_setup(self) -> None:
        if not self._initial_setup_pending:
            return
        self._initial_setup_pending = False
        self.present_phone_settings()

    def _on_open_message_requested(self, _client, handle: str) -> None:
        self._phone_dialog.close()
        self.present()
        self.messages.open_message(handle)

    def _on_availability(self, _client, available: bool) -> None:
        if available:
            self._configured = True
        self._banner.set_revealed(self._configured and not available)
