"""Exercise real GTK switches on a private Broadway display, without Bluetooth."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest


@pytest.mark.private_dbus
def test_gtk_explicit_pairing_controls(tmp_path):
    executable = shutil.which("gtk4-broadwayd")
    if executable is None:
        pytest.skip("GTK4 Broadway is not installed")
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    environment = dict(
        os.environ, XDG_RUNTIME_DIR=str(runtime), GDK_BACKEND="broadway",
        BROADWAY_DISPLAY=":0", GTK_A11Y="none", GSETTINGS_BACKEND="memory",
        PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
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
            [sys.executable, str(Path(__file__).resolve())], env=environment,
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


if __name__ == "__main__":
    _exercise_gtk_controls()
