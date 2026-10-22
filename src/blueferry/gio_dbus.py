"""Asynchronous D-Bus client calls through Gio.DBusConnection.

The daemon still talks to most services through dbus-python, which guesses
argument types from Python values and makes a blocking call the easy default.
GDBus has neither problem: every call carries a ``GLib.Variant`` built from an
explicit type string, the reply is checked against an expected type, and the
API is asynchronous unless one deliberately picks ``call_sync``.

``GioBus`` is the narrow seam modules migrate to (see ARCHITECTURE.md,
"D-Bus calls"). It exposes one asynchronous ``call`` and one ``subscribe``;
tests replace it with a fake that records the explicit types.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Protocol

from gi.repository import Gio, GLib

log = logging.getLogger(__name__)

# What dbus-python reports for an unanswered call; keeps error names stable
# for callers that compare them.
NO_REPLY = "org.freedesktop.DBus.Error.NoReply"
FAILED = "org.freedesktop.DBus.Error.Failed"


class DBusCallError(Exception):
    """A failed call, named like a D-Bus error (``org.bluez.Error.Failed``)."""

    def __init__(self, name: str, message: str = "") -> None:
        super().__init__(f"{name}: {message}" if message else name)
        self.name = name
        self.message = message

    def get_dbus_name(self) -> str:
        """Same accessor as ``dbus.exceptions.DBusException``."""
        return self.name


def call_error(error: GLib.Error) -> DBusCallError:
    """Translate a GDBus ``GLib.Error`` into a named ``DBusCallError``."""
    remote = Gio.DBusError.get_remote_error(error)
    message = error.message or ""
    if remote:
        # strip_remote_error edits the C struct, not PyGObject's copy.
        prefix = f"GDBus.Error:{remote}: "
        return DBusCallError(remote, message.removeprefix(prefix))
    if error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.TIMED_OUT):
        return DBusCallError(NO_REPLY, message)
    return DBusCallError(FAILED, message)


class Subscription(Protocol):
    def remove(self) -> None: ...


class GioBus(Protocol):
    def call(
        self, bus_name: str, object_path: str, interface: str, method: str,
        signature: str, args: tuple, reply_type: str,
        on_reply: Callable[..., None], on_error: Callable[[Exception], None],
        *, timeout: float,
    ) -> None: ...

    def subscribe(
        self, sender: str, interface: str, member: str,
        handler: Callable[..., None], *, path: str | None = None,
        arg0: str | None = None, with_path: bool = False,
    ) -> Subscription: ...


class _SignalSubscription:
    def __init__(self, connection: Gio.DBusConnection, subscription_id: int) -> None:
        self._connection = connection
        self._id: int | None = subscription_id

    def remove(self) -> None:
        if self._id is not None:
            self._connection.signal_unsubscribe(self._id)
            self._id = None


class GioDBus:
    """``GioBus`` over a lazily fetched ``Gio.DBusConnection``.

    Replies and signals are dispatched on the thread-default main context of
    the thread that made the call or subscription: the GLib main loop for
    daemon code.
    """

    def __init__(self, connection: Callable[[], Gio.DBusConnection]) -> None:
        self._connection = connection

    def call(
        self, bus_name: str, object_path: str, interface: str, method: str,
        signature: str, args: tuple, reply_type: str,
        on_reply: Callable[..., None], on_error: Callable[[Exception], None],
        *, timeout: float,
    ) -> None:
        """Call ``interface.method`` with ``args`` typed as ``(signature)``.

        ``reply_type`` is the full reply tuple type, e.g. ``"()"`` or
        ``"(a{oa{sa{sv}}})"``; GDBus fails the call if the reply differs.
        ``on_reply`` receives the reply's members as positional arguments.
        A type mismatch in ``args`` raises ``TypeError`` before dispatch.
        """
        parameters = GLib.Variant(f"({signature})", args)
        connection = self._connection()

        def finished(source: Gio.DBusConnection, result: Gio.AsyncResult) -> None:
            try:
                reply = source.call_finish(result)
            except GLib.Error as error:
                on_error(call_error(error))
                return
            on_reply(*reply.unpack())

        connection.call(
            bus_name, object_path, interface, method, parameters,
            GLib.VariantType.new(reply_type), Gio.DBusCallFlags.NONE,
            int(timeout * 1000), None, finished,
        )

    def subscribe(
        self, sender: str, interface: str, member: str,
        handler: Callable[..., None], *, path: str | None = None,
        arg0: str | None = None, with_path: bool = False,
    ) -> Subscription:
        """Deliver ``member`` signals as unpacked positional arguments.

        With ``with_path`` the emitting object path comes first, for
        subscriptions that match more than one object.
        """
        connection = self._connection()

        def received(
            _connection: Gio.DBusConnection, _sender: str, object_path: str,
            _interface: str, _member: str, parameters: GLib.Variant,
        ) -> None:
            if with_path:
                handler(object_path, *parameters.unpack())
            else:
                handler(*parameters.unpack())

        subscription_id = connection.signal_subscribe(
            sender, interface, member, path, arg0, Gio.DBusSignalFlags.NONE, received,
        )
        return _SignalSubscription(connection, subscription_id)


def system_bus() -> Gio.DBusConnection:
    """The process-wide GDBus system-bus connection.

    Connecting is synchronous but happens once, on first use; GDBus caches
    the connection afterwards.
    """
    return Gio.bus_get_sync(Gio.BusType.SYSTEM, None)


def error_name(error: object) -> str:
    """D-Bus error name of ``error`` or its type name, bounded for logs."""
    getter: Any = getattr(error, "get_dbus_name", None)
    if callable(getter):
        return str(getter() or "")[:256]
    return type(error).__name__[:256]
