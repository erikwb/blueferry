"""Thin, asynchronous system-bus transport to oFono.

Every method call is sent asynchronously so the daemon's GLib loop never
waits for oFono or the phone. Calls address the well-known name directly
instead of creating a proxy object: dbus-python proxies resolve (and may try
to activate) the owner synchronously, which would block when oFono is absent.
The message is built by hand so it can carry NO_AUTO_START: BlueFerry must
never cause the system bus to activate oFono (as root) on its behalf; a
missing oFono simply answers ServiceUnknown.
Signal watches use ``add_signal_receiver``, which tracks the owner
asynchronously. The controller receives this class through a small protocol so
tests substitute an inert fake and never reach a bus.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any, Protocol

import dbus
import dbus.exceptions
import dbus.lowlevel

from blueferry.bus import get_system_bus
from blueferry.calls.model import OFONO_SERVICE

log = logging.getLogger(__name__)

# oFono answers local property and call-control requests quickly, but
# Modem.Powered waits for the HFP service-level connection with the phone and
# Dial waits for the phone to accept ATD. Keep one bounded ceiling for both.
OFONO_CALL_TIMEOUT_SEC = 30.0

_SERVICE_MISSING = frozenset({
    "org.freedesktop.DBus.Error.ServiceUnknown",
    "org.freedesktop.DBus.Error.NameHasNoOwner",
    "org.freedesktop.DBus.Error.NoServer",
    "org.freedesktop.DBus.Error.Disconnected",
    "org.freedesktop.DBus.Error.FileNotFound",
    "org.freedesktop.DBus.Error.Spawn.ServiceNotFound",
})
# oFono's shipped policy (ofono.conf, in /etc or /usr/share dbus-1/system.d)
# allows only root and at_console users;
# a daemon started outside a console session gets AccessDenied.
_ACCESS_DENIED = frozenset({
    "org.freedesktop.DBus.Error.AccessDenied",
})
_INTERFACE_MISSING = frozenset({
    "org.freedesktop.DBus.Error.UnknownObject",
    "org.freedesktop.DBus.Error.UnknownInterface",
    "org.freedesktop.DBus.Error.UnknownMethod",
})

Reply = Callable[..., None]
Failure = Callable[[Exception], None]


class SignalMatch(Protocol):
    def remove(self) -> object: ...


class OfonoTransport(Protocol):
    def call(
        self,
        path: str,
        interface: str,
        method: str,
        signature: str,
        args: Sequence[Any],
        on_reply: Reply,
        on_error: Failure,
    ) -> None: ...

    def watch(
        self,
        handler: Callable[..., None],
        *,
        interface: str,
        signal: str,
        path: str | None = None,
    ) -> SignalMatch: ...

    def watch_owner(self, handler: Callable[[bool], None]) -> SignalMatch: ...


def error_name(error: Exception) -> str:
    if isinstance(error, dbus.exceptions.DBusException):
        return str(error.get_dbus_name() or "")
    return ""


def service_missing(error: Exception) -> bool:
    """True when oFono (or the system bus) is simply not there."""
    return error_name(error) in _SERVICE_MISSING


def access_denied(error: Exception) -> bool:
    """True when oFono's D-Bus policy refuses this user."""
    return error_name(error) in _ACCESS_DENIED


def interface_missing(error: Exception) -> bool:
    """True when an oFono object or interface disappeared under us."""
    return error_name(error) in _INTERFACE_MISSING


def public_error(error: Exception) -> str:
    """A short log-safe description; oFono messages can contain numbers."""
    name = error_name(error)
    return name[:128] if name else type(error).__name__


class DBusOfonoTransport:
    """dbus-python implementation bound to the daemon's system bus."""

    def __init__(self, bus_factory: Callable[[], Any] = get_system_bus) -> None:
        self._bus_factory = bus_factory

    def call(
        self,
        path: str,
        interface: str,
        method: str,
        signature: str,
        args: Sequence[Any],
        on_reply: Reply,
        on_error: Failure,
    ) -> None:
        message = dbus.lowlevel.MethodCallMessage(
            destination=OFONO_SERVICE, path=path, interface=interface, method=method,
        )
        if args:
            message.append(*args, signature=signature)
        message.set_auto_start(False)

        def replied(reply: Any) -> None:
            # Same mapping as dbus-python's call_async.
            if isinstance(reply, dbus.lowlevel.MethodReturnMessage):
                on_reply(*reply.get_args_list())
            elif isinstance(reply, dbus.lowlevel.ErrorMessage):
                on_error(dbus.exceptions.DBusException(
                    *reply.get_args_list(), name=reply.get_error_name(),
                ))
            else:
                on_error(TypeError(f"unexpected reply type: {type(reply).__name__}"))

        self._bus_factory().send_message_with_reply(
            message, replied, OFONO_CALL_TIMEOUT_SEC, require_main_loop=True,
        )

    def watch(
        self,
        handler: Callable[..., None],
        *,
        interface: str,
        signal: str,
        path: str | None = None,
    ) -> SignalMatch:
        return self._bus_factory().add_signal_receiver(
            handler,
            signal_name=signal,
            dbus_interface=interface,
            bus_name=OFONO_SERVICE,
            path=path,
        )

    def watch_owner(self, handler: Callable[[bool], None]) -> SignalMatch:
        return self._bus_factory().add_signal_receiver(
            lambda _name, _old, new: handler(bool(new)),
            signal_name="NameOwnerChanged",
            dbus_interface="org.freedesktop.DBus",
            bus_name="org.freedesktop.DBus",
            path="/org/freedesktop/DBus",
            arg0=OFONO_SERVICE,
        )


def boolean(value: bool) -> dbus.Boolean:
    return dbus.Boolean(value)


def byte(value: int) -> dbus.Byte:
    return dbus.Byte(value)
