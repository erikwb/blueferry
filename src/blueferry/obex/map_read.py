"""Best-effort MAP write-back of message read state to the iPhone."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable

import dbus
import dbus.exceptions

from blueferry.bus import obex

log = logging.getLogger(__name__)

_HANDLE_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def set_message_read(message_path: str) -> None:
    """Set ``org.bluez.obex.Message1.Read`` on one live Message1 object."""
    obex(message_path, "org.freedesktop.DBus.Properties").Set(
        "org.bluez.obex.Message1", "Read", dbus.Boolean(True), timeout=10.0
    )


def set_session_messages_read(session_path: str, handles: Iterable[str]) -> None:
    """Mark reconstructed Message1 objects read on the current MAP session.

    Handles are history-opaque path tails. After a session reconnect they may
    no longer exist; failures are expected and ignored.
    """
    root = str(session_path or "").rstrip("/")
    if not root:
        return
    for handle in handles:
        value = str(handle or "")
        if value in {".", ".."} or not _HANDLE_RE.fullmatch(value):
            continue
        path = f"{root}/{value}"
        try:
            set_message_read(path)
        except dbus.exceptions.DBusException as error:
            log.debug("MAP mark-read failed for %s: %s", value, error)
