"""Closed private connections cannot end the test process."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.private_dbus

_ROOT = Path(__file__).resolve().parent.parent

# Opens one connection, closes it, and dispatches the default context. With
# libdbus's exit-on-disconnect still set, that dispatch calls _exit(1). Run in
# a child process so a regression fails this test instead of ending pytest.
_PROBE = """
import os, sys
if sys.argv[1] == "conftest":
    import tests.conftest  # noqa: F401
import dbus, dbus.bus
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib
DBusGMainLoop(set_as_default=True)
connection = {
    "session-positional": lambda: dbus.SessionBus(True),
    "system-keyword": lambda: dbus.SystemBus(private=True),
    "address": lambda: dbus.bus.BusConnection(os.environ["DBUS_SESSION_BUS_ADDRESS"]),
}[sys.argv[2]]()
connection.close()
context = GLib.MainContext.default()
for _ in range(20):
    context.iteration(False)
print("survived")
"""


def _probe(mode: str, constructor: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(_ROOT / "src"), str(_ROOT)])
    return subprocess.run(
        [sys.executable, "-c", _PROBE, mode, constructor],
        cwd=_ROOT, env=env, capture_output=True, text=True, timeout=60,
        check=False,
    )


def test_closed_private_connection_exits_without_the_conftest_wrapper() -> None:
    # The hazard the wrapper exists for; if libdbus ever stops doing this,
    # the wrapper can go.
    result = _probe("plain", "session-positional")
    assert result.returncode == 1
    assert "survived" not in result.stdout


@pytest.mark.parametrize(
    "constructor", ["session-positional", "system-keyword", "address"],
)
def test_conftest_disables_exit_on_disconnect_for_every_connection(
    constructor,
) -> None:
    result = _probe("conftest", constructor)
    assert result.returncode == 0, result.stderr
    assert "survived" in result.stdout
