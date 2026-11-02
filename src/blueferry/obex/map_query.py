"""Direct MAP queries against the iPhone — list recent messages from a
folder, fetch full message bodies on demand.

Used by the DBus service so the CLI can show the iPhone's actual inbox
history (not just locally cached events since daemon startup).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

import dbus
import dbus.exceptions

from blueferry.bus import obex
from blueferry.events import normalize_phone, parse_map_timestamp
from blueferry.limits import (
    MAX_RECENT_QUERY_LIMIT,
    MAX_REMOTE_PROPERTY_CHARS,
    MAX_THREAD_BODY_CHARS,
)
from blueferry.otp_context import OtpMetadata

log = logging.getLogger(__name__)

QUERY_DEADLINE_SECONDS = 60
OTP_QUERY_DEADLINE_SECONDS = 20
OTP_QUERY_LIMIT = 20


def lookup_otp_metadata(session_path: str, message_path: str) -> OtpMetadata | None:
    """Read the phone's time and read flag for one push, on the OBEX worker.

    MNS notifications often omit Timestamp. Listing the inbox populates it,
    but the newest entry might be another message. Fail closed unless the
    exact object path appears in a small, bounded listing of this session.
    """
    parent, _, handle = message_path.rpartition("/")
    if parent != session_path or not handle.startswith("message"):
        return None
    deadline = time.monotonic() + OTP_QUERY_DEADLINE_SECONDS
    map_iface = obex(session_path, "org.bluez.obex.MessageAccess1")
    _navigate_to_folder(map_iface, "telecom/msg/INBOX", deadline=deadline)
    messages = map_iface.ListMessages(
        "", {"MaxListCount": dbus.UInt16(OTP_QUERY_LIMIT)},
        timeout=_remaining(deadline, 10),
    )
    # ListMessages returns a{oa{sv}} with the same Message1 properties that
    # GetAll exposes. No body download or read-state write is necessary.
    props = messages.get(message_path)
    if props is None:
        return None
    timestamp = props.get("Timestamp")
    read = props.get("Read")
    return OtpMetadata(
        parse_map_timestamp(timestamp) if isinstance(timestamp, str) else None,
        # Missing or malformed flags cannot establish that the code is unread.
        bool(read) if isinstance(read, (bool, dbus.Boolean)) else True,
    )


def _bounded_text(value: object, maximum: int) -> str:
    text = str(value or "")
    if len(text) <= maximum:
        return text
    return text[:max(0, maximum - 1)] + ("…" if maximum else "")


def _remaining(deadline: float, maximum: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("MAP message query exceeded its operation deadline")
    return min(maximum, remaining)


def _navigate_to_folder(
    map_iface: dbus.Interface, folder: str, *, deadline: float,
) -> None:
    """SetFolder to an absolute folder like 'telecom/msg/INBOX'."""
    try:
        map_iface.SetFolder("/", timeout=_remaining(deadline, 10))
    except dbus.exceptions.DBusException:
        pass
    for seg in folder.split("/"):
        if not seg:
            continue
        map_iface.SetFolder(seg, timeout=_remaining(deadline, 10))


def list_recent_messages(
    session_path: str,
    folder: str = "telecom/msg/INBOX",
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return the most recent `limit` message summaries from `folder`.

    Each dict has: handle, sender, sender_phone_norm,
    body, timestamp, read, status, type, folder.
    """
    limit = max(1, min(int(limit), 200))
    deadline = time.monotonic() + QUERY_DEADLINE_SECONDS
    map_iface = obex(session_path, "org.bluez.obex.MessageAccess1")
    _navigate_to_folder(map_iface, folder, deadline=deadline)

    # ListMessages returns object paths; we fetch each one's Message1 props
    paths = list(map_iface.ListMessages(
        "", {"MaxListCount": dbus.UInt16(limit)},
        timeout=_remaining(deadline, 30),
    ))[:limit]
    log.info("ListMessages(%s, limit=%d) → %d paths", folder, limit, len(paths))

    out: list[dict[str, Any]] = []
    for p in paths:
        path_s = str(p)
        try:
            raw = dict(
                obex(path_s, "org.freedesktop.DBus.Properties")
                .GetAll(
                    "org.bluez.obex.Message1",
                    timeout=_remaining(deadline, 10),
                )
            )
        except dbus.exceptions.DBusException as e:
            log.debug("skip %s: %s", path_s, e.get_dbus_name())
            continue
        sender_value = raw.get("Sender") or raw.get("SenderAddress")
        sender_raw = (
            _bounded_text(sender_value, MAX_REMOTE_PROPERTY_CHARS)
            if sender_value is not None else None
        )
        ts = parse_map_timestamp(raw.get("Timestamp"))
        out.append({
            "handle": path_s.rsplit("/", 1)[-1],
            "sender": sender_raw or "",
            "sender_phone_norm": normalize_phone(sender_raw) or "",
            "body": _bounded_text(raw.get("Subject"), MAX_THREAD_BODY_CHARS),
            "timestamp": ts.isoformat() if ts else "",
            "read": bool(raw.get("Read", False)),
            "status": _bounded_text(raw.get("Status"), MAX_REMOTE_PROPERTY_CHARS),
            "type": _bounded_text(raw.get("Type"), MAX_REMOTE_PROPERTY_CHARS),
            "folder": _bounded_text(
                raw.get("Folder"), MAX_REMOTE_PROPERTY_CHARS
            ) or folder,
        })
    return out


# MAP's FilterPeriodBegin format. BlueZ passes the string through unchanged,
# and the MAP specification reads it in the phone's local time.
_PERIOD_FORMAT = "%Y%m%dT%H%M%S"
INBOX_FOLDER = "telecom/msg/INBOX"


def list_inbox_since(
    session_path: str,
    since: datetime | None,
    limit: int,
) -> list[dict[str, Any]]:
    """List inbox summaries no older than ``since``, on the OBEX worker.

    Returns the raw, bounded listing fields; the caller validates and
    deduplicates them. BlueZ's ListMessages filter keys are ``MaxCount``
    and ``PeriodBegin``. Whether iOS honours the period filter is not
    established, so the caller filters by timestamp again. The listing
    already carries every needed property, so no ``GetAll`` per message and
    no bMessage download (which could change the phone's read state) is made.
    """
    bounded = max(1, min(int(limit), MAX_RECENT_QUERY_LIMIT))
    deadline = time.monotonic() + QUERY_DEADLINE_SECONDS
    map_iface = obex(session_path, "org.bluez.obex.MessageAccess1")
    _navigate_to_folder(map_iface, INBOX_FOLDER, deadline=deadline)
    filters = dbus.Dictionary(
        {"MaxCount": dbus.UInt16(bounded)}, signature="sv",
    )
    if since is not None:
        filters["PeriodBegin"] = dbus.String(
            since.astimezone().strftime(_PERIOD_FORMAT)
        )
    listing = map_iface.ListMessages(
        "", filters, timeout=_remaining(deadline, 30),
    )
    out: list[dict[str, Any]] = []
    for path, props in listing.items():
        if len(out) >= bounded:
            break
        path_s = str(path)
        if not path_s.startswith(f"{session_path}/"):
            continue
        raw = dict(props)
        read = raw.get("Read")
        timestamp = raw.get("Timestamp")
        out.append({
            "handle": path_s.rsplit("/", 1)[-1],
            "sender_address": _bounded_text(
                raw.get("SenderAddress"), MAX_REMOTE_PROPERTY_CHARS
            ),
            "sender_name": _bounded_text(
                raw.get("Sender"), MAX_REMOTE_PROPERTY_CHARS
            ),
            "body": _bounded_text(raw.get("Subject"), MAX_THREAD_BODY_CHARS),
            "timestamp": (
                _bounded_text(timestamp, 64) if isinstance(timestamp, str) else ""
            ),
            # Missing or malformed flags are reported as unknown.
            "read": bool(read) if isinstance(read, (bool, dbus.Boolean)) else None,
            "type": _bounded_text(raw.get("Type"), MAX_REMOTE_PROPERTY_CHARS),
        })
    return out
