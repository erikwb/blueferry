"""Main application window: the phone card above the Messages, Calls,
Notifications and Photos tabs.

GTK counterpart of the Qt client's main page (``Main.qml`` with
``PhoneCard.qml``). The tabs live in an ``AdwViewStack``; the switcher sits
in the header bar and moves to a bottom bar on narrow windows. Opt-in data
is read only while its tab is visible to the user (see
:meth:`MainWindow.sync_watches`); tabs are added with
:meth:`MainWindow.add_tab`. What the Qt card shows below its switches,
"From Plugins" and "Tools", unfolds below the card strip with
**Plugins & Tools**.
"""

from __future__ import annotations

from collections.abc import Callable

from gi.repository import Adw, Gio, Gtk

from blueferry.client_tasks import TaskRunner
from blueferry.gio_dbus import GioBus
from blueferry.i18n import _
from blueferry.setup_client import SetupClient
from blueferry.tel_links import dial_request
from blueferry.ui.calls import CallsPage
from blueferry.ui.companion_tools import CompanionToolsSection, ToolsFactory, default_tools
from blueferry.ui.conversations import ConversationsPage
from blueferry.ui.dial_presenter import dial_prompt
from blueferry.ui.notifications import NotificationsPage
from blueferry.ui.phone_card import PhoneCard, load_phone_name
from blueferry.ui.photos import PhotosPage
from blueferry.ui.plugin_cards import (
    FileChooser,
    PluginCardSection,
    SurfacesFactory,
    choose_files,
    default_surfaces,
)
from blueferry.ui.status import SettingsDialog

COMPACT_WIDTH = "max-width: 650px"


class MainWindow(Adw.ApplicationWindow):
    def __init__(
        self, application, client, *, name_loader=load_phone_name, timer=None,
        surfaces: SurfacesFactory | None = None, tools: ToolsFactory | None = None,
        photo_runners: tuple[TaskRunner, TaskRunner] | None = None,
        card_bus: GioBus | None = None, files: FileChooser = choose_files,
    ) -> None:
        super().__init__(application=application, title=_("BlueFerry"))
        self._client = client
        self._dial_request_serial = 0
        self._dial_dialog: Adw.AlertDialog | None = None
        self._closed = False
        # name -> set_watched(bool) of every tab that reads while shown.
        self._watchers: dict[str, Callable[[bool], None]] = {}
        self.set_default_size(940, 660)

        self._toasts = Adw.ToastOverlay()
        self._configured = SetupClient().configuration().configured
        self._initial_setup_pending = not self._configured
        self.settings = SettingsDialog(client)

        self.stack = Adw.ViewStack(vexpand=True)
        self.messages = ConversationsPage(client, self.toast)
        self.add_tab("messages", _("Messages"), "chat-message-new-symbolic", self.messages)
        self.calls = CallsPage(
            client, self.toast, dial=self.request_dial,
            compose=self.compose_message, open_settings=self.present_phone_settings,
            timer=timer,
        )
        self.add_tab("calls", _("Calls"), "call-start-symbolic", self.calls,
                     self.calls.set_watched)
        self.notifications = NotificationsPage(
            client, open_settings=self.present_phone_settings, timer=timer,
        )
        self.add_tab(
            "notifications", _("Notifications"),
            "preferences-system-notifications-symbolic", self.notifications,
            self.notifications.set_watched,
        )
        list_runner, original_runner = photo_runners or (
            client.task_runner("photos"), client.task_runner("photo-originals"))
        self.photos = PhotosPage(list_runner, original_runner)
        self.add_tab("photos", _("Photos"), "folder-pictures-symbolic", self.photos,
                     self.photos.set_watched)
        self.stack.connect("notify::visible-child-name", lambda *_args: self.sync_watches())

        self.phone_card = PhoneCard(client, self.toast, name_loader=name_loader)
        self.phone_card.add_status_listener(self.calls.apply_status)
        self.phone_card.add_status_listener(self.notifications.apply_status)

        # "From Plugins" and "Tools": below the card strip, on demand.
        self.plugin_cards = PluginCardSection(
            surfaces or default_surfaces(client), bus=card_bus, files=files,
        )
        self.tools = CompanionToolsSection(tools or default_tools(client), timer=timer)
        self.extras_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=18, homogeneous=True,
            margin_top=6, margin_bottom=12, margin_start=12, margin_end=12,
        )
        self.extras_box.append(self.plugin_cards)
        self.extras_box.append(self.tools)
        self.extras = Gtk.Revealer(child=Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True,
            max_content_height=300, child=self.extras_box,
        ))
        self.extras_button = Gtk.ToggleButton(
            child=Adw.ButtonContent(
                label=_("Plugins & Tools"), icon_name="application-x-addon-symbolic"),
            valign=Gtk.Align.CENTER, css_classes=["flat"],
            tooltip_text=_("Show what plugins offer and the companion tools"),
        )
        self.extras_button.connect("toggled", self._extras_toggled)
        self.phone_card.append(self.extras_button)
        self._plugin_set: tuple | None = None
        self.settings.plugins.add_listener(self._plugins_changed)

        self.switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        self.header = Adw.HeaderBar(title_widget=self.switcher)
        menu = Gio.Menu()
        menu.append(_("Settings"), "win.phone")
        menu.append(_("Keyboard Shortcuts"), "app.shortcuts")
        menu.append(_("About BlueFerry"), "app.about")
        menu_button = Gtk.MenuButton(
            icon_name="open-menu-symbolic",
            menu_model=menu,
            tooltip_text=_("Main Menu"),
        )
        menu_button.update_property([Gtk.AccessibleProperty.LABEL], [_("Main Menu")])
        self.header.pack_end(menu_button)

        phone_action = Gio.SimpleAction.new("phone", None)
        phone_action.connect("activate", lambda *_args: self.present_phone_settings())
        self.add_action(phone_action)

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
        content.append(self.phone_card)
        content.append(self.extras)
        content.append(Gtk.Separator())
        content.append(self.stack)
        self._toasts.set_child(content)

        self.switcher_bar = Adw.ViewSwitcherBar(stack=self.stack)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(self.header)
        toolbar.set_content(self._toasts)
        toolbar.add_bottom_bar(self.switcher_bar)
        self.set_content(toolbar)

        # Narrow: the conversations collapse, the switcher moves to the
        # bottom, and the card and Calls tab stack their parts vertically.
        compact = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(COMPACT_WIDTH))
        compact.add_setter(self.messages.split_view, "collapsed", True)
        compact.add_setter(self.messages.back_button, "visible", True)
        compact.add_setter(self.header, "title-widget", Adw.WindowTitle(title=_("BlueFerry")))
        compact.add_setter(self.switcher_bar, "reveal", True)
        compact.add_setter(self.phone_card, "orientation", Gtk.Orientation.VERTICAL)
        compact.add_setter(self.phone_card, "spacing", 6)
        compact.add_setter(self.extras_box, "orientation", Gtk.Orientation.VERTICAL)
        compact.add_setter(self.calls.columns, "orientation", Gtk.Orientation.VERTICAL)
        compact.add_setter(self.calls.separator, "orientation", Gtk.Orientation.HORIZONTAL)
        # One scrolled page: the recent calls follow the dial pad.
        compact.add_setter(self.calls.recent_scroll, "vscrollbar-policy", Gtk.PolicyType.NEVER)
        self.add_breakpoint(compact)

        for signal in ("notify::visible", "notify::suspended", "notify::is-active"):
            self.connect(signal, lambda *_args: self.sync_watches())
        self.connect("close-request", self._on_close_request)
        client.connect("availability-changed", self._on_availability)
        client.connect("open-message-requested", self._on_open_message_requested)
        self._on_availability(client, client.available)

    # ---- tabs -----------------------------------------------------------

    def add_tab(
        self, name: str, title: str, icon: str, widget: Gtk.Widget,
        set_watched: Callable[[bool], None] | None = None,
    ) -> Adw.ViewStackPage:
        """Add a tab; ``set_watched(shown)`` follows its visibility."""
        page = self.stack.add_titled_with_icon(widget, name, title, icon)
        if set_watched is not None:
            self._watchers[name] = set_watched
            self.sync_watches()
        return page

    def show_tab(self, name: str) -> None:
        self.stack.set_visible_child_name(name)

    @property
    def shown(self) -> bool:
        """The window can be seen: mapped, not minimized or covered (suspended)."""
        return not self._closed and self.get_visible() and not self.is_suspended()

    def sync_watches(self) -> None:
        """Let only the visible tab of a shown window read its private data."""
        shown = self.shown
        visible = self.stack.get_visible_child_name()
        if hasattr(self, "phone_card"):
            self.phone_card.set_watched(shown)
        if hasattr(self, "tools"):
            # Each look runs idevice_id: only while someone sees the section.
            self.tools.set_watched(
                shown and self.is_active() and self.extras.get_reveal_child())
        for name, set_watched in self._watchers.items():
            set_watched(shown and name == visible)

    def _extras_toggled(self, button: Gtk.ToggleButton) -> None:
        self.extras.set_reveal_child(button.get_active())
        self.sync_watches()

    def _plugins_changed(self, state: dict) -> None:
        """Enabling, installing or removing a plugin changes the card and
        may take the photo plugin away (as in the Qt controller)."""
        if not state.get("loaded"):
            return
        current = tuple((row.get("id"), row.get("enabled")) for row in state.get("plugins", []))
        if current == self._plugin_set:
            return
        known = self._plugin_set is not None
        self._plugin_set = current
        if known:
            self.plugin_cards.schedule_reload()
            self.photos.model.plugins_changed()

    def _on_close_request(self, _window) -> bool:
        # The window is destroyed after this: stop reading, cancel timers.
        self._closed = True
        self.sync_watches()
        self.plugin_cards.stop()
        return False

    # ---- feedback -------------------------------------------------------

    def toast(self, text: str) -> None:
        self._toasts.add_toast(Adw.Toast(title=text, use_markup=False))

    def compose_message(self, address: str) -> None:
        self.show_tab("messages")
        self.messages.compose_to(address)

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
        self.show_tab("messages")
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
