"""blueferry-tray: pure presenter logic plus an offscreen smoke test."""
from __future__ import annotations

import importlib.util
import os
import subprocess  # nosec B404 - runs the smoke script in a fresh process
import sys
import textwrap
from pathlib import Path

import pytest

from blueferry.models import BackendStatus, Thread
from blueferry.qt import tray_presenter as presenter

ROOT = Path(__file__).resolve().parents[1]

# The smoke tests start the real tray in a child process. Distributions
# without PySide6 (the Ubuntu package build) skip them; the presenter tests
# stay pure Python.
needs_pyside6 = pytest.mark.skipif(
    importlib.util.find_spec("PySide6") is None, reason="PySide6 is not installed",
)


def _thread(*read_flags: bool, outgoing: bool = False) -> Thread:
    return Thread.from_dict({
        "key": "t",
        "messages": [
            {"handle": str(index), "body": "", "read": read, "outgoing": outgoing}
            for index, read in enumerate(read_flags)
        ],
    })


def test_unread_total_counts_incoming_unread_messages_only() -> None:
    threads = [_thread(False, True, False), _thread(False, outgoing=True), _thread(True)]
    assert presenter.unread_total(threads) == 2
    assert presenter.unread_total([]) == 0


@pytest.mark.parametrize(("count", "text"), [(0, ""), (-1, ""), (1, "1"), (99, "99"), (100, "99+")])
def test_badge_text(count: int, text: str) -> None:
    assert presenter.badge_text(count) == text


def test_tooltip_lists_connection_battery_signal_network_and_unread() -> None:
    assert presenter.tooltip(None, 0) == "BlueFerry service is not running"
    assert presenter.tooltip(None, 0, "needs update") == "needs update"
    status = BackendStatus.from_dict({
        "map": True, "phone_battery_level": 87, "phone_battery_source": "gatt",
        "phone_signal_strength": 60, "phone_network_name": "Sunrise",
        "phone_network_status": "registered",
    })
    assert presenter.tooltip(status, 3).splitlines() == [
        "iPhone connected", "Battery: 87 %", "Signal: 60 %", "Network: Sunrise",
        "3 unread messages",
    ]
    offline = BackendStatus.from_dict({"phone_battery_level": 40, "phone_battery_source": "hfp"})
    assert presenter.tooltip(offline, 1).splitlines() == [
        "iPhone offline", "Battery: about 40 %", "1 unread message",
    ]
    connecting = BackendStatus.from_dict({"initializing": True})
    assert presenter.tooltip(connecting, 0) == "Connecting…"


_SMOKE = textwrap.dedent("""
    import json, sys, time
    from pathlib import Path
    from PySide6.QtDBus import QDBusConnection, QDBusMessage
    from PySide6.QtWidgets import QApplication
    from blueferry.protocol import EVENTS_IFACE, MESSAGES_API_VERSION
    from blueferry.qt.tray import TrayController, badged_icon, phone_icon

    app = QApplication([])
    address = sys.argv[1]
    bus = (QDBusConnection.connectToBus(address, "tray-smoke") if address
           else QDBusConnection("not-connected"))
    launches = []
    entry = Path(sys.argv[2]) / "autostart" / "io.weirdware.BlueFerry.Tray.desktop"
    refreshes, thread_loads = [], []

    class CountingTray(TrayController):
        def refresh(self):
            refreshes.append(1)
            super().refresh()

        def refresh_threads(self):
            thread_loads.append(1)
            super().refresh_threads()

    tray = CountingTray(
        bus, launch=lambda: launches.append(1) or True, autostart_path=entry,
    )
    # The subscriptions only fail without a bus; then the tray polls.
    assert tray.signals_connected is bool(address), tray.signals_connected
    assert tray._fallback_timer.isActive() is (not address)
    tray.tray.setVisible(True)  # so hiding must come from the bus reply
    tray.start()
    for _ in range(100):  # the ListActivatableNames reply is asynchronous
        app.processEvents()
        time.sleep(0.01)
    # Private test bus: BlueFerry is not activatable, so the icon hides.
    # No bus at all: unknown, so it stays.
    assert tray.tray.isVisible() is (not address), tray.tray.isVisible()
    # No daemon on the bus: offline, and nothing was activated.
    assert tray.status is None, tray.status
    assert tray.tray.toolTip() == "BlueFerry service is not running", tray.tray.toolTip()

    tray.apply_status(json.dumps({
        "api_version": MESSAGES_API_VERSION, "map": True,
        "phone_battery_level": 87, "phone_battery_source": "gatt",
    }))
    tray.apply_threads(json.dumps([{"key": "a", "messages": [
        {"handle": "1", "body": "", "read": False},
        {"handle": "2", "body": "", "read": False}]}]))
    lines = tray.tray.toolTip().splitlines()
    assert lines == ["iPhone connected", "Battery: 87 %", "2 unread messages"], lines
    texts = [action.text() for action in tray.menu.actions() if not action.isSeparator()]
    assert texts == ["Open BlueFerry", "Start at login", "Quit"], texts
    # Autostart is opt-in: off until the user checks it, then a user entry.
    assert not tray.autostart_action.isChecked() and not entry.exists()
    tray.autostart_action.trigger()
    assert tray.autostart_action.isChecked() and entry.is_file()
    tray.autostart_action.trigger()
    assert not tray.autostart_action.isChecked() and not entry.exists()

    # A daemon restart (new owner) reloads the unread count with the next
    # status, even when the coalesced refresh still saw the old status.
    status = json.dumps({"api_version": MESSAGES_API_VERSION, "map": True})
    thread_loads.clear()
    tray.apply_status(status)
    assert thread_loads == [], thread_loads
    tray._owner_changed("io.weirdware.BlueFerry", ":1.1", ":1.2")
    tray.apply_status(status)
    assert thread_loads == [1], thread_loads
    tray.apply_status(status)
    assert thread_loads == [1], thread_loads

    if address:
        # A content-free StatusChanged from the daemon's owner refreshes.
        daemon = QDBusConnection.connectToBus(address, "tray-smoke-daemon")
        assert daemon.registerService("io.weirdware.BlueFerry")
        for _ in range(50):
            app.processEvents()
            time.sleep(0.01)
        refreshes.clear()
        signal = QDBusMessage.createSignal(
            "/io/weirdware/BlueFerry", EVENTS_IFACE, "StatusChanged",
        )
        assert daemon.send(signal)
        for _ in range(100):
            app.processEvents()
            time.sleep(0.01)
            if refreshes:
                break
        assert refreshes, "StatusChanged did not refresh the tray"
        daemon.unregisterService("io.weirdware.BlueFerry")
        tray.apply_status(status)

    tray.apply_status(json.dumps({"api_version": 0}))
    assert "needs an update" in tray.tray.toolTip()

    tray.open_app()  # no Qt client registered: start one
    assert launches == [1]

    badge = badged_icon(phone_icon(), "3").pixmap(64, 64).toImage()
    red = badge.pixelColor(56, 6)
    assert red.red() > 180 and red.green() < 120, red.name()
    assert badged_icon(phone_icon(), "").pixmap(64, 64).toImage().pixelColor(63, 0).alpha() == 0
    tray.close()
    print("SMOKE-OK")
""")


@needs_pyside6
@pytest.mark.parametrize("use_private_bus", [False, True])
def test_tray_smoke_offscreen(use_private_bus: bool, tmp_path: Path) -> None:
    address = os.environ.get("BLUEFERRY_TEST_DBUS_ADDRESS", "") if use_private_bus else ""
    if use_private_bus and not address:
        pytest.skip("needs the private dbus-run-session test bus")
    env = {
        **os.environ,
        "QT_QPA_PLATFORM": "offscreen",
        "PYTHONPATH": str(ROOT / "src"),
        "LANGUAGE": "C",
        "XDG_CONFIG_HOME": str(tmp_path),
    }
    if not use_private_bus:
        env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/tmp/blueferry-tests-no-live-bus"
    result = subprocess.run(  # nosec B603 - fixed interpreter and script
        [sys.executable, "-c", _SMOKE, address, str(tmp_path)],
        env=env, capture_output=True, text=True, timeout=60, check=False,
    )
    assert "SMOKE-OK" in result.stdout, result.stdout + result.stderr


@needs_pyside6
@pytest.mark.parametrize(("choice", "output", "exists"), [
    ("status", "off", False), ("on", "on", True), ("off", "off", False),
])
def test_autostart_command_line(tmp_path: Path, choice: str, output: str, exists: bool) -> None:
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "src"),
        "LANGUAGE": "C",
        "XDG_CONFIG_HOME": str(tmp_path),
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/blueferry-tests-no-live-bus",
    }
    if choice == "off":
        (tmp_path / "autostart").mkdir()
        (tmp_path / "autostart" / "io.weirdware.BlueFerry.Tray.desktop").write_text("")
    result = subprocess.run(  # nosec B603 - fixed interpreter and module
        [sys.executable, "-m", "blueferry.qt.tray", f"--autostart={choice}"],
        env=env, capture_output=True, text=True, timeout=60, check=False,
    )
    assert (result.returncode, result.stdout.strip()) == (0, output), result.stderr
    assert (tmp_path / "autostart" / "io.weirdware.BlueFerry.Tray.desktop").exists() is exists


_RACE_SETUP = textwrap.dedent("""
    import json, sys
    from pathlib import Path
    from PySide6.QtDBus import QDBusConnection
    from PySide6.QtWidgets import QApplication
    from blueferry.protocol import BUS_NAME, MESSAGES_API_VERSION
    from blueferry.qt.tray import TrayController

    app = QApplication([])
    calls = []
    class DeferredTray(TrayController):
        def _call(self, service, interface, method, args, reply, error, **kwargs):
            calls.append((method, reply, error))
    tray = DeferredTray(QDBusConnection("not-connected"), autostart_path=Path(sys.argv[1]) / "tray.desktop")
    tray._daemon_owned = True
    status = json.dumps({"api_version": MESSAGES_API_VERSION, "map": True})
    threads = json.dumps([{"key": "a", "messages": [{"handle": "1", "read": False}]}])
""")


@needs_pyside6
@pytest.mark.parametrize("scenario", [
    "late_status", "late_threads", "status_burst", "threads_burst", "late_installed",
    "closed",
])
def test_tray_delayed_replies(tmp_path, scenario):
    cases = {
        "late_status": """
            tray.refresh()
            reply = calls[-1][1]
            tray._owner_changed(BUS_NAME, ":1.1", "")
            reply([status])
            assert tray.status is None
            assert tray.unread == 0
        """,
        "late_threads": """
            tray.apply_status(status)
            reply = calls[-1][1]
            tray._owner_changed(BUS_NAME, ":1.1", "")
            tray.apply_offline()
            reply([threads])
            assert tray.unread == 0
        """,
        "status_burst": """
            for _ in range(1000):
                tray.refresh()
            assert len(calls) == 1, len(calls)
            calls[0][1]([status])
            assert tray._refresh_timer.isActive()
            tray.refresh()
            assert len([c for c in calls if c[0] == "GetStatus"]) == 2
        """,
        "threads_burst": """
            tray.apply_status(status)
            for _ in range(1000):
                tray.refresh_threads()
            assert len(calls) == 1, len(calls)
            calls[0][1]([threads])
            assert tray._threads_timer.isActive()
        """,
        "late_installed": """
            tray._daemon_owned = False
            # Exercise the asynchronous installed check even on the inert bus.
            tray._bus = type("Bus", (), {"isConnected": lambda self: True})()
            tray.refresh()
            reply = calls[-1][1]
            tray._owner_changed(BUS_NAME, "", ":1.2")
            tray.refresh()
            assert tray.tray.isVisible()
            reply([[]])
            assert tray.tray.isVisible()
        """,
        "closed": """
            tray.refresh()
            reply = calls[-1][1]
            tray.close()
            reply([status])
            assert tray.status is None
            assert not tray._refresh_timer.isActive()
            assert not tray._threads_timer.isActive()
            assert not tray._fallback_timer.isActive()
        """,
    }
    script = _RACE_SETUP + textwrap.dedent(cases[scenario]) + '\nprint("RACE-OK")\n'
    result = subprocess.run(  # nosec B603 - fixed interpreter and regression script
        [sys.executable, "-c", script, str(tmp_path)],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONPATH": str(ROOT / "src"),
             "DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/blueferry-tests-no-live-bus"},
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0 and "RACE-OK" in result.stdout, result.stdout + result.stderr
