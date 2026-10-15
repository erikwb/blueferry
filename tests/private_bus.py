"""Private connections to the isolated test bus that cannot kill pytest."""
from __future__ import annotations

import dbus


def open_private_bus(kind: str = "session", **kwargs):
    """Open a private test-bus connection with exit-on-disconnect disabled.

    dbus-python leaves libdbus's exit-on-disconnect enabled on private
    connections. Once such a connection is closed, the next dispatch of its
    queued Disconnected message calls ``_exit(1)``. A failing test keeps its
    closed connections alive in the traceback, so the next GLib iteration
    ends the pytest process silently: no traceback, no summary, only
    ``F`` and exit status 1.
    """
    factory = {"session": dbus.SessionBus, "system": dbus.SystemBus}[kind]
    connection = factory(private=True, **kwargs)
    connection.set_exit_on_disconnect(False)
    return connection
