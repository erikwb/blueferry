"""Exercise GTK pairing controls and errors on a private Broadway display."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest


@pytest.mark.private_dbus
@pytest.mark.parametrize("scenario", ["controls", "error-toasts", "activation", "upgrade-activation"])
def test_gtk_pairing_ui(tmp_path, scenario):
    executable = shutil.which("gtk4-broadwayd")
    if executable is None:
        pytest.skip("GTK4 Broadway is not installed")
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    environment = dict(
        os.environ, XDG_RUNTIME_DIR=str(runtime), GDK_BACKEND="broadway",
        BROADWAY_DISPLAY=":0", GTK_A11Y="none", GSETTINGS_BACKEND="memory",
        PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
        XDG_CONFIG_HOME=str(tmp_path / "config"),
    )
    # Both the GTK display and its HTTP endpoint use private Unix sockets.
    daemon = subprocess.Popen(
        [executable, "--unixsocket", str(runtime / "http"), ":0"],
        env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 5
        while not (runtime / "broadway1.socket").exists():
            assert daemon.poll() is None, daemon.communicate()[0].decode()
            assert time.monotonic() < deadline, "Broadway did not create its display socket"
            time.sleep(0.02)
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), scenario], env=environment,
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        daemon.terminate()
        try:
            daemon.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            daemon.kill()
            daemon.communicate()


def _exercise_gtk_controls():
    # Run GTK in its own process: the suite also loads Qt and must not share
    # toolkit event loops or adopt the operator's Wayland/X11 display.
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace
    from unittest.mock import patch

    from blueferry.bluetooth_devices import PairedDevice
    from blueferry.setup_client import BluetoothCompatibility, ConfigurationState
    from blueferry.ui import status

    status.Gtk.init()
    status.Gtk.Settings.get_default().set_property("gtk-enable-animations", False)
    calls, operations, dialogs = [], [], []
    configuration = ConfigurationState.from_dict({"configured": False, "saved": False})
    setup = SimpleNamespace(
        configuration=lambda: configuration,
        complete_isolated=lambda mac, **options: calls.append((mac, options)),
    )

    class Dialog:
        def __init__(self, **_kwargs):
            dialogs.append(self)

        def connect(self, _signal, callback):
            self.respond = lambda: callback(self, "replace")

        def __getattr__(self, _name):
            return lambda *_args: None

    with (
        patch.object(status, "SetupClient", lambda: setup),
        patch.object(status.IPhonePage, "_load_setup_state", lambda self: None),
        patch.object(status.Adw, "AlertDialog", Dialog),
    ):
        page = status.IPhonePage(SimpleNamespace(connect=lambda *_args: None), lambda *_args: None)
        page._run_setup = lambda operation, _done: operations.append(operation)
        page._devices = [PairedDevice.from_dict({
            "mac": "NEW", "name": "Phone", "paired": False,
            "adapter_path": "/org/bluez/hci1",
        })]
        page._device_model.append("Phone")
        page._device_row.set_selected(0)
        for compatibility in (False, True):
            for explicit in (False, True):
                for replace in (False, True):
                    for row_click in (False, True):
                        calls.clear()
                        operations.clear()
                        dialogs.clear()
                        page._configuration = ConfigurationState.from_dict({
                            "configured": False, "saved": replace,
                            "mac": "OLD" if replace else "",
                        })
                        capabilities = BluetoothCompatibility.from_dict({
                            "adapter": "hci1", "notifications_supported": not compatibility,
                            "explicit_pairing_default": not explicit,
                        })
                        page._explicit_pairing_overrides.clear()
                        page._apply_compatibility(capabilities)
                        switch = page._explicit_pairing_switch
                        assert switch.get_active() is not explicit
                        if row_click:
                            page._explicit_pairing_row.activate()
                        else:
                            switch.activate()
                        assert switch.get_active() is explicit
                        # Refreshing capabilities and switching adapters must
                        # preserve the option actually chosen by the user.
                        page._apply_compatibility(capabilities)
                        page._apply_compatibility(BluetoothCompatibility.from_dict({
                            "adapter": "hci0", "notifications_supported": True,
                        }))
                        page._apply_compatibility(capabilities)
                        assert switch.get_active() is explicit
                        page._pair_button.emit("clicked")
                        if replace:
                            assert len(dialogs) == 1 and not operations
                            # Background refreshes can still change controls
                            # while the replacement confirmation is open.
                            switch.set_active(not explicit)
                            dialogs[0].respond()
                        assert len(operations) == 1
                        switch.set_active(not explicit)

                        def forbid_worker_read():
                            raise AssertionError("Pairing worker read a GTK switch")

                        with (
                            patch.object(switch, "get_active", forbid_worker_read),
                            patch.object(page._compatibility_switch, "get_active", forbid_worker_read),
                            ThreadPoolExecutor(max_workers=1) as worker,
                        ):
                            worker.submit(operations[0]).result(timeout=5)
                        assert len(calls) == 1
                        mac, options = calls[0]
                        assert mac == "NEW" and options["adapter"] == "hci1"
                        assert options["explicit_pairing"] is explicit
                        assert options["compatibility_mode"] is compatibility
                        assert options["replace_saved_mac"] == ("OLD" if replace else "")


def _exercise_error_toasts():
    import io
    import traceback
    from types import SimpleNamespace
    from unittest.mock import patch

    from gi.repository import GLib

    from blueferry import setup_client
    from blueferry.ui.window import Adw, Gtk, MainWindow

    Adw.init()
    try:
        raise RuntimeError("Could not prepare <iPhone> & Bluetooth")
    except RuntimeError:
        diagnostic = traceback.format_exc()
    # Real tracebacks contain markup-like names such as <module>, and errors
    # can contain literal angle brackets and ampersands as well.
    process = SimpleNamespace(
        stdin=io.StringIO(), stdout=iter(()), stderr=io.StringIO(diagnostic),
        wait=lambda **_kwargs: 1, poll=lambda: 1,
    )
    with patch.object(setup_client.subprocess, "Popen", return_value=process):
        try:
            setup_client.SetupClient().complete_isolated(
                "02:00:00:00:00:01", confirmation=lambda _passkey: True,
            )
        except setup_client.PairingError as error:
            message = f"Setup failed: {error}"
        else:
            raise AssertionError("The failed helper must raise PairingError")
    assert diagnostic.strip() in message

    def label_texts(widget):
        if isinstance(widget, Gtk.Label):
            yield widget.get_text()
        child = widget.get_first_child()
        while child is not None:
            yield from label_texts(child)
            child = child.get_next_sibling()

    for show_toast in (MainWindow.toast, MainWindow.phone_toast):
        overlay = Adw.ToastOverlay(child=Gtk.Label(label="Test content"))
        window = Gtk.Window(default_width=680, default_height=620, child=overlay)
        try:
            window.present()
            show_toast(SimpleNamespace(_toasts=overlay, _phone_toasts=overlay), message)
            context = GLib.MainContext.default()
            while context.pending():
                context.iteration(False)
            assert message in list(label_texts(overlay)), "GTK lost the diagnostic text"
        finally:
            window.destroy()


def _exercise_gtk_activation():
    import threading
    from types import SimpleNamespace
    from unittest.mock import patch

    from blueferry import client_activation
    from blueferry.ui import app as app_module

    calls, errors = [], []

    class Window(app_module.Adw.ApplicationWindow):
        def __init__(self, application, client):
            super().__init__(application=application)

        def set_startup_id(self, token):
            calls.append(("token", token))
            super().set_startup_id(token)

        def open_message(self, handle):
            calls.append(("message", handle))
            self.present()

        def present_initial_setup(self):
            calls.append(("setup", ""))

    def request_existing():
        try:
            assert client_activation.open_message("existing", "warm-token")
        except Exception as error:
            errors.append(error)
        finally:
            app_module.GLib.idle_add(app.quit)

    def after_startup():
        threading.Thread(target=request_existing, daemon=True).start()
        return False

    with (
        patch.object(app_module, "MainWindow", Window),
        patch.object(app_module, "DaemonClient", lambda: SimpleNamespace(stop=lambda: None)),
        patch.dict(os.environ, {"XDG_ACTIVATION_TOKEN": "cold-token"}),
    ):
        app = app_module.BlueFerryApp()
        app_module.GLib.timeout_add(100, after_startup)
        app_module.GLib.timeout_add_seconds(10, app.quit)
        assert app.run(["blueferry-gtk", "--message", "cold"]) == 0
    assert not errors, errors
    assert calls == [
        ("token", "cold-token"), ("message", "cold"),
        ("token", "warm-token"), ("message", "existing"),
    ], calls


def _serve_activation_backend():
    from types import SimpleNamespace

    import dbus.service
    from gi.repository import GLib

    from blueferry.bus import get_session_bus
    from blueferry.dbus_service import MessagesService
    from blueferry.protocol import BUS_NAME, EVENTS_IFACE, OBJECT_PATH

    bus = get_session_bus()
    name = dbus.service.BusName(BUS_NAME, bus=bus, do_not_queue=True)
    service = MessagesService(name, SimpleNamespace(map=None, pbap=None, map_path=None))
    # A second process listening for the same backend signal must not be
    # activated by the legacy relay. The GTK process has two bus connections.
    observer = dbus.SessionBus(private=True)
    match = observer.add_signal_receiver(
        lambda handle: print("unexpected-broadcast:" + str(handle), flush=True),
        signal_name="OpenMessageRequested", dbus_interface=EVENTS_IFACE,
        bus_name=BUS_NAME, path=OBJECT_PATH,
    )
    print("ready", flush=True)
    loop = GLib.MainLoop()
    GLib.timeout_add_seconds(25, loop.quit)
    try:
        loop.run()
    finally:
        match.remove()
        observer.close()
        service.close()


def _exercise_gtk_upgrade_activation():
    import select
    import threading

    import gi
    gi.require_version("Adw", "1")
    gi.require_version("Gtk", "4.0")
    from gi.repository import Adw, Gio, GLib, Gtk

    from blueferry.bus import get_session_bus
    from blueferry.protocol import BUS_NAME, EVENTS_IFACE, OBJECT_PATH

    backend = subprocess.Popen(
        [sys.executable, __file__, "activation-backend"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
    )
    calls, errors = [], []
    done = threading.Event()

    class LegacyApp(Adw.Application):
        """The shipped GTK interface before --message/Client.Gtk existed."""
        def __init__(self):
            super().__init__(
                application_id="io.weirdware.BlueFerry.Gtk",
                flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
            )
            self.window = None

        def do_startup(self):
            Adw.Application.do_startup(self)
            self.match = get_session_bus().add_signal_receiver(
                self.open_message, signal_name="OpenMessageRequested", dbus_interface=EVENTS_IFACE,
                bus_name=BUS_NAME, path=OBJECT_PATH,
            )

        def do_activate(self):
            if self.window is None:
                self.draft = Gtk.Entry(text="unsent draft")
                self.window = Adw.ApplicationWindow(application=self, content=self.draft)
            self.window.present()

        def open_message(self, handle):
            calls.append(str(handle))
            self.window.present()

    def activate_from_new_client():
        try:
            commands = [
                [sys.executable, "-m", "blueferry.ui.app", "--message=from-new-gtk"],
                [sys.executable, "-m", "blueferry.client_activation", "--message=from-notification"],
                [sys.executable, "-m", "blueferry.ui.app"],
            ]
            for command in commands:
                result = subprocess.run(command, capture_output=True, text=True, timeout=10)
                assert result.returncode == 0, result.stdout + result.stderr
        except Exception as error:
            errors.append(error)
        finally:
            done.set()

    def start_requests():
        threading.Thread(target=activate_from_new_client, daemon=True).start()
        return False

    def check_done():
        if done.is_set() and (errors or len(calls) == 2):
            app.quit()
            return False
        return True

    try:
        assert select.select([backend.stdout], [], [], 5)[0], "backend did not start"
        assert backend.stdout.readline() == b"ready\n"
        app = LegacyApp()
        GLib.timeout_add(100, start_requests)
        GLib.timeout_add(10, check_done)
        GLib.timeout_add_seconds(20, app.quit)
        assert app.run(["blueferry-gtk"]) == 0
        assert not errors, errors
        assert calls == ["from-new-gtk", "from-notification"], calls
        assert app.draft.get_text() == "unsent draft"
        app.match.remove()
    finally:
        backend.terminate()
        output, error = backend.communicate(timeout=5)
        assert b"unexpected-broadcast" not in output, output
        assert not error, error


if __name__ == "__main__":
    if sys.argv[1] == "controls":
        _exercise_gtk_controls()
    elif sys.argv[1] == "error-toasts":
        _exercise_error_toasts()
    elif sys.argv[1] == "upgrade-activation":
        _exercise_gtk_upgrade_activation()
    elif sys.argv[1] == "activation-backend":
        _serve_activation_backend()
    else:
        _exercise_gtk_activation()
