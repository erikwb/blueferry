"""BlueFerry GTK4 / libadwaita desktop app entry point.

A separate process from the daemon. Its application id is
`io.weirdware.BlueFerry.Gtk` — distinct from the daemon's bus name
`io.weirdware.BlueFerry`, which it talks to over D-Bus.
"""

from __future__ import annotations

import logging
import os
import sys

import dbus
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from blueferry import __version__  # noqa: E402
from blueferry.client_activation import forward_to_legacy_gtk, record_client_use  # noqa: E402
from blueferry.glib_client_activation import ClientActivation  # noqa: E402
from blueferry.i18n import _  # noqa: E402
from blueferry.ui.client import DaemonClient  # noqa: E402
from blueferry.ui.window import MainWindow  # noqa: E402

APP_ID = "io.weirdware.BlueFerry.Gtk"
APP_ICON = "io.weirdware.BlueFerry"

_CSS = """
.msg-bubble { padding: 6px 10px; }
.msg-out {
  background: @accent_bg_color;
  color: @accent_fg_color;
}
"""


class BlueFerryApp(Adw.Application):
    def __init__(self) -> None:
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE | Gio.ApplicationFlags.SEND_ENVIRONMENT,
        )
        self.add_main_option(
            "message", 0, GLib.OptionFlags.NONE, GLib.OptionArg.STRING,
            "Open the conversation containing a message handle", "HANDLE",
        )
        self._client: DaemonClient | None = None
        self._activation: ClientActivation | None = None

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        self._activation = ClientActivation("gtk", self._open_message)
        for name, callback in (
            ("about", self._show_about),
            ("shortcuts", self._show_shortcuts),
            ("quit", lambda *_args: self.quit()),
            ("close", self._close_window),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)
        self.set_accels_for_action("app.quit", ["<primary>q"])
        self.set_accels_for_action("app.close", ["<primary>w"])
        self.set_accels_for_action("app.shortcuts", ["<primary>question"])
        self.set_accels_for_action("win.phone", ["<primary>comma"])

        provider = Gtk.CssProvider()
        provider.load_from_string(_CSS)
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

    def _close_window(self, *_args) -> None:
        if self.props.active_window is not None:
            self.props.active_window.close()

    def _show_about(self, *_args) -> None:
        dialog = Adw.AboutDialog(
            application_name=_("BlueFerry"),
            application_icon=APP_ICON,
            version=__version__,
            developer_name=_("BlueFerry contributors"),
            website="https://github.com/erikwb/blueferry",
            issue_url="https://github.com/erikwb/blueferry/issues",
            copyright=_(
                "Copyright © 2026 Erik Bourget <erik@ebourget.net>\n"
                "Copyright © 2026 Gabe Shatunovsky <gabriel@shatunovsky.com>"
            ),
            license_type=Gtk.License.GPL_2_0_ONLY,
        )
        dialog.present(self.props.active_window)

    def _show_shortcuts(self, *_args) -> None:
        dialog = Adw.AlertDialog(
            heading=_("Keyboard Shortcuts"),
            body=_(
                "Ctrl+W  Close Window\n"
                "Ctrl+Q  Quit\n"
                "Ctrl+,  iPhone Settings\n"
                "Ctrl+?  Keyboard Shortcuts\n"
                "Enter   Send Message"
            ),
        )
        dialog.add_response("close", _("Close"))
        dialog.set_default_response("close")
        dialog.present(self.props.active_window)

    def do_activate(self) -> None:
        self._open_message("", "")

    def do_handle_local_options(self, options: GLib.VariantDict) -> int:
        handle = options.lookup_value("message", None)
        token = os.environ.get("XDG_ACTIVATION_TOKEN") or os.environ.get("DESKTOP_STARTUP_ID", "")
        try:
            forwarded = forward_to_legacy_gtk(handle.unpack() if handle is not None else "", token)
        except dbus.DBusException:
            logging.getLogger(__name__).exception("could not activate the existing GTK client")
            return 1
        return -1 if forwarded is None else 0 if forwarded else 1

    def do_command_line(self, command_line: Gio.ApplicationCommandLine) -> int:
        handle = command_line.get_options_dict().lookup_value("message", None)
        token = (
            command_line.getenv("XDG_ACTIVATION_TOKEN")
            or command_line.getenv("DESKTOP_STARTUP_ID") or ""
        )
        self._open_message(handle.unpack() if handle is not None else "", token)
        return 0

    def _open_message(self, handle: str, token: str) -> None:
        win = self.props.active_window
        if win is None:
            if self._client is None:
                self._client = DaemonClient()
            win = MainWindow(application=self, client=self._client)
            win.connect("notify::is-active", self._window_active_changed)
        if token:
            win.set_startup_id(token)
        if handle:
            win.open_message(handle)
        else:
            win.present()
        record_client_use("gtk")
        if not handle:
            win.present_initial_setup()
        # A later manual activation must not accidentally reuse this token.
        os.environ.pop("XDG_ACTIVATION_TOKEN", None)
        os.environ.pop("DESKTOP_STARTUP_ID", None)

    def _window_active_changed(self, win: Gtk.Window, _pspec) -> None:
        if win.is_active():
            record_client_use("gtk")

    def do_shutdown(self) -> None:
        if self._activation is not None:
            self._activation.close()
        if self._client is not None:
            self._client.stop()
        Adw.Application.do_shutdown(self)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    return BlueFerryApp().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
