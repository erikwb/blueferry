"""D-Bus connections and GLib integration shared by the backend."""
from __future__ import annotations

import threading

import dbus
import dbus.mainloop
import dbus.mainloop.glib
from gi.repository import GLib

dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
# The OBEX connection is created and used on a dedicated worker thread.
# dbus-python requires GLib threading support to be initialized before any
# connection is shared with a process that dispatches D-Bus from threads.
dbus.mainloop.glib.threads_init()

_thread_state = threading.local()


def _worker_mainloop():
    """Keep synchronous worker connections out of GTK's GLib dispatcher."""
    if threading.current_thread() is threading.main_thread():
        return None
    return dbus.mainloop.NULL_MAIN_LOOP


def get_system_bus() -> dbus.SystemBus:
    """Return a system-bus connection owned by the current thread."""
    connection = getattr(_thread_state, "system_bus", None)
    if connection is None:
        # dbus-python's default shared connection is process-wide. GTK setup
        # work runs beside the GLib UI loop, and dispatching that connection
        # on one thread while another uses it can corrupt libdbus state.
        connection = dbus.SystemBus(
            private=True,
            mainloop=_worker_mainloop(),
        )
        _thread_state.system_bus = connection
    return connection


def get_session_bus() -> dbus.SessionBus:
    """Return a session-bus connection owned by the current thread."""
    connection = getattr(_thread_state, "session_bus", None)
    if connection is None:
        connection = dbus.SessionBus(
            private=True,
            mainloop=_worker_mainloop(),
        )
        _thread_state.session_bus = connection
    return connection

main_loop: GLib.MainLoop = GLib.MainLoop()


def initialize_obex_worker_bus() -> None:
    """Give the current worker thread its own session-bus connection.

    dbus-python connections are not a useful synchronization boundary: a
    synchronous call made on the GLib connection can still hold up the code
    dispatching the daemon's public API.  The OBEX worker therefore owns a
    private connection and is the only thread that performs slow profile I/O.
    """
    _thread_state.obex_bus = dbus.SessionBus(private=True)


def close_obex_worker_bus() -> None:
    """Close the current worker thread's private connection, if any."""
    bus = getattr(_thread_state, "obex_bus", None)
    if bus is None:
        return
    try:
        bus.close()
    finally:
        del _thread_state.obex_bus


def obex(path: str, iface: str) -> dbus.Interface:
    """Return an interface on a BlueZ OBEX session object."""
    bus = getattr(_thread_state, "obex_bus", None) or get_session_bus()
    return dbus.Interface(
        bus.get_object("org.bluez.obex", path), iface
    )


def bluez(path: str, iface: str) -> dbus.Interface:
    """Return an interface on a system-bus BlueZ object."""
    return dbus.Interface(get_system_bus().get_object("org.bluez", path), iface)
