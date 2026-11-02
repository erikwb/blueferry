"""Exported dbus-python objects that check argument types before dispatch."""
from __future__ import annotations

import dbus.lowlevel
import dbus.service


class StrictObject(dbus.service.Object):
    """Reject calls whose arguments do not match the method's in_signature.

    dbus-python dispatches without checking a method's in_signature, so a
    method would see whatever the caller sent: bool() reads the string
    "false" as true, and str() accepts any value at all.
    """

    def _message_cb(self, connection, message) -> None:
        if isinstance(message, dbus.lowlevel.MethodCallMessage):
            try:
                _method, declared = dbus.service._method_lookup(
                    self, message.get_member(), message.get_interface()
                )
            except Exception:
                # The base class repeats the lookup and replies with its error.
                declared = None
            expected = getattr(declared, "_dbus_in_signature", None)
            if expected is not None and message.get_signature() != expected:
                if not message.get_no_reply():
                    connection.send_message(dbus.lowlevel.ErrorMessage(
                        message, "org.freedesktop.DBus.Error.InvalidArgs",
                        f"expected arguments of type '{expected}'",
                    ))
                return
        super()._message_cb(connection, message)
