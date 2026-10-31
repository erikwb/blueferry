"""Main application window."""

from __future__ import annotations

from gi.repository import Adw, Gio, Gtk

from blueferry.i18n import _
from blueferry.setup_client import SetupClient
from blueferry.tel_links import dial_request
from blueferry.ui.conversations import ConversationsPage
from blueferry.ui.dial_presenter import dial_prompt
from blueferry.ui.phone import PhonePage
from blueferry.ui.status import SettingsDialog


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, application, client) -> None:
        super().__init__(application=application, title=_("BlueFerry"))
        self._client = client
        self._dial_request_serial = 0
        self._dial_dialog: Adw.AlertDialog | None = None
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
        menu.append(_("iPhone"), "win.phone-overview")
        menu.append(_("Settings"), "win.phone")
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

        overview_action = Gio.SimpleAction.new("phone-overview", None)
        overview_action.connect("activate", lambda *_args: self.present_phone_overview())
        self.add_action(overview_action)
        self._overview_toasts = Adw.ToastOverlay()
        self.phone_overview = PhonePage(client, self.overview_toast)
        self._overview_toasts.set_child(self.phone_overview)
        overview_toolbar = Adw.ToolbarView()
        overview_toolbar.add_top_bar(
            Adw.HeaderBar(title_widget=Adw.WindowTitle(title=_("iPhone")))
        )
        overview_toolbar.set_content(self._overview_toasts)
        self._overview_dialog = Adw.Dialog(
            title=_("iPhone"),
            content_width=560,
            content_height=600,
            child=overview_toolbar,
        )
        self._overview_dialog.connect(
            "closed", lambda _dialog: self.phone_overview.set_watching(False)
        )

        self.settings = SettingsDialog(client)

        self._banner = Adw.Banner(
            title=_("Background service unavailable — open Settings to repair it"),
            button_label=_("Open Settings"),
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
        self._toasts.add_toast(Adw.Toast(title=text, use_markup=False))

    def overview_toast(self, text: str) -> None:
        self._overview_toasts.add_toast(Adw.Toast(title=text, use_markup=False))

    def present_phone_overview(self) -> None:
        self.phone_overview.set_watching(True)
        self._overview_dialog.present(self)

    def present_phone_settings(self, category: str = "") -> None:
        self.settings.open(self, category)

    def present_initial_setup(self) -> None:
        if not self._initial_setup_pending:
            return
        self._initial_setup_pending = False
        self.present_phone_settings("phone")

    def _on_open_message_requested(self, _client, handle: str) -> None:
        self.open_message(handle)

    def open_message(self, handle: str) -> None:
        self.settings.close()
        self._overview_dialog.close()
        self.present()
        self.messages.open_message(handle)

    def request_dial(self, link: str) -> None:
        """Confirm a clicked phone link; only the Call response dials."""
        self._dial_request_serial += 1
        serial = self._dial_request_serial
        if self._dial_dialog is not None:
            old_dialog, self._dial_dialog = self._dial_dialog, None
            old_dialog.force_close()
        number, problem = dial_request(link)

        def present(status) -> None:
            # A second link supersedes both an open dialog and an outstanding
            # status read. Late success/error replies must not revive it.
            if serial == self._dial_request_serial:
                self._show_dial_prompt(number, problem, status)

        if problem:
            present(None)
            return
        self._client.get_status_async(
            present, lambda _message: present(None),
        )

    def _show_dial_prompt(self, number: str, problem: str, status) -> None:
        name = self.messages.contact_name_for(number) if number else ""
        prompt = dial_prompt(number, problem, name, status)
        dialog = Adw.AlertDialog(heading=prompt.heading, body=prompt.body)
        self._dial_dialog = dialog
        if prompt.offer == "none":
            dialog.add_response("close", _("Close"))
            dialog.set_default_response("close")
            dialog.set_close_response("close")
        else:
            dialog.add_response("cancel", _("Cancel"))
            dialog.set_close_response("cancel")
            if prompt.offer == "call":
                dialog.add_response("call", _("Call"))
                dialog.set_response_appearance("call", Adw.ResponseAppearance.SUGGESTED)
            else:
                dialog.add_response("settings", _("Open iPhone Settings"))
            # Enter must not place a call on its own.
            dialog.set_default_response("cancel")

        def responded(_dialog, response: str) -> None:
            if self._dial_dialog is not dialog:
                return
            self._dial_dialog = None
            if response == "call" and prompt.offer == "call":
                self._client.dial_async(
                    number,
                    lambda _call_id: self.toast(_("Calling {number}…").format(number=number)),
                    lambda message: self.toast(message),
                )
            elif response == "settings" and prompt.offer == "settings":
                self.present_phone_settings("calls")

        dialog.connect("response", responded)
        dialog.present(self)

    def _on_availability(self, _client, available: bool) -> None:
        if available:
            self._configured = True
        self._banner.set_revealed(self._configured and not available)
