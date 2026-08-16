"""MAP outgoing message send (SMS + iMessage on iOS 26.5).

As recorded in PROTOCOL.md, MessageAccess1.PushMessage
with a properly-formed bMessage works on iOS 26.5 — and when the recipient
is iMessage-capable, iOS routes the outgoing as iMessage automatically
(blue bubble). The same code path handles SMS too.

Public surface:
    send_message(session_path, recipient, body) -> str (transfer path)
    send_group_message(session_path, recipients, body) -> str (transfer path)

Caller (typically the daemon's DBus service) owns the MAP session and
just passes its path here.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Sequence

import dbus
import dbus.exceptions

from blueferry.bus import obex
from blueferry.limits import MAX_OUTGOING_BODY_BYTES
from blueferry.obex.transfer import wait_for_transfer
from blueferry.private_files import create_runtime_private_file
from blueferry.recipients import InvalidRecipient, validate_recipient

log = logging.getLogger(__name__)

def _byte_stuff(body: str) -> str:
    """Apply MAP bMessage byte-stuffing.

    Lines in the message body that start with `BEGIN:`, `END:`, or other
    keywords the bMessage parser would intercept must be prefixed with a
    single space. Conservative implementation: prefix any line starting
    with `BEGIN:` or `END:`.
    """
    return "\n".join(
        (" " + line)
        if line.upper().startswith(("BEGIN:", "END:")) else line
        for line in body.splitlines()
    )


def _recipient_vcard(recipient: str) -> list[str]:
    """Return one validated bMessage recipient vCard as structural lines."""
    recipient = validate_recipient(recipient)
    recipient_property = "EMAIL" if "@" in recipient else "TEL"
    return [
        "BEGIN:VCARD",
        "VERSION:2.1",
        "N:;;;;",
        f"{recipient_property}:{recipient}",
        "END:VCARD",
    ]


def _build_bmessage(recipients: Sequence[str], body: str) -> str:
    """Build a bMessage from recipients that have already been validated."""
    body_size = len(body.encode("utf-8"))
    if body_size > MAX_OUTGOING_BODY_BYTES:
        raise ValueError(
            f"message body exceeds {MAX_OUTGOING_BODY_BYTES} UTF-8 bytes"
        )
    recipient_lines: list[str] = []
    for recipient in recipients:
        recipient_lines.extend(_recipient_vcard(recipient))

    stuffed = _byte_stuff(body)
    encoded_len = len(stuffed.encode("utf-8"))
    lines = [
        "BEGIN:BMSG",
        "VERSION:1.0",
        "STATUS:UNREAD",
        "TYPE:SMS_GSM",
        "FOLDER:telecom/msg/outbox",
        # Originator
        "BEGIN:VCARD",
        "VERSION:2.1",
        "N:;;;;",
        "TEL:",
        "END:VCARD",
        "BEGIN:BENV",
        *recipient_lines,
        "BEGIN:BBODY",
        "CHARSET:UTF-8",
        f"LENGTH:{encoded_len}",
        "BEGIN:MSG",
        stuffed,
        "END:MSG",
        "END:BBODY",
        "END:BENV",
        "END:BMSG",
    ]
    return "\r\n".join(lines) + "\r\n"


def build_bmessage(recipient: str, body: str) -> str:
    """Return a single-recipient bMessage suitable for MAP PushMessage.

    Structure per Bluetooth MAP 1.4 spec:
      • Originator VCARD (empty for outgoing — iPhone fills in)
      • BENV → recipient VCARD + BBODY → MSG body

    Phone recipients use the MAP `TEL` vCard property. Apple-ID/email
    recipients use `EMAIL`, while retaining `TYPE:SMS_GSM`: iOS exposes
    iMessage through the SMS-shaped MAP path and performs the final routing.

    Raises InvalidRecipient if `recipient` isn't a plain phone number or a
    conservative ASCII email address.
    """
    return _build_bmessage([validate_recipient(recipient)], body)


def build_group_bmessage(recipients: Sequence[str], body: str) -> str:
    """Return one MAP bMessage containing two or more recipient vCards.

    MAP's bMessage envelope permits repeated recipient vCards.  On the tested
    iPhone/iOS 26.5, a matching participant set routes the reply into the
    existing iMessage group rather than creating individual conversations.
    """
    normalized = [validate_recipient(recipient) for recipient in recipients]
    if not 2 <= len(normalized) <= 20:
        raise InvalidRecipient("a group message requires 2 to 20 recipients")
    if len(set(normalized)) != len(normalized):
        raise InvalidRecipient("group recipients must be unique")
    return _build_bmessage(normalized, body)


def _push_bmessage(
    session_path: str,
    bmsg: str,
    body: str,
    *,
    folder: str,
    poll_timeout_s: float,
) -> str:
    """Write and push an already-validated bMessage."""
    fd, tmp = create_runtime_private_file(
        prefix="blueferry_send_", suffix=".bmsg"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = -1
            stream.write(bmsg)
        map_iface = obex(session_path, "org.bluez.obex.MessageAccess1")
        log.info("PushMessage (%d-byte body, folder=%s)", len(body), folder)
        try:
            options = dbus.Dictionary({}, signature="sv")
            ret = map_iface.PushMessage(
                str(tmp), folder, options, timeout=30.0
            )
        except dbus.exceptions.DBusException as e:
            raise RuntimeError(
                f"PushMessage rejected: {e.get_dbus_name()}: "
                f"{e.get_dbus_message() or '(no message)'}"
            ) from e
        transfer_path = str(ret[0]) if isinstance(ret, tuple | list) else str(ret)
        log.info("transfer: %s", transfer_path)

        initial = dict(ret[1]) if isinstance(ret, tuple | list) and len(ret) > 1 else {}
        status = wait_for_transfer(
            transfer_path,
            initial_status=str(initial.get("Status", "queued")),
            timeout_s=poll_timeout_s,
        )
        log.info("send result: status=%s", status)
        return transfer_path
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def send_message(
    session_path: str,
    recipient: str,
    body: str,
    *,
    folder: str = "telecom/msg/outbox",
    poll_timeout_s: float = 30.0,
) -> str:
    """Push a message via the given MAP session.

    Returns the BlueZ transfer object path once status reaches 'complete'
    or 'gone'. Raises on InvalidArguments or transfer error.
    """
    # Raises InvalidRecipient before we touch DBus. Idempotent, so
    # build_bmessage re-validating below is harmless.
    recipient = validate_recipient(recipient)
    bmsg = build_bmessage(recipient, body)
    return _push_bmessage(
        session_path, bmsg, body,
        folder=folder, poll_timeout_s=poll_timeout_s,
    )


def send_group_message(
    session_path: str,
    recipients: Sequence[str],
    body: str,
    *,
    folder: str = "telecom/msg/outbox",
    poll_timeout_s: float = 30.0,
) -> str:
    """Push one group reply as a bMessage with multiple recipient vCards."""
    normalized = [validate_recipient(recipient) for recipient in recipients]
    bmsg = build_group_bmessage(normalized, body)
    return _push_bmessage(
        session_path, bmsg, body,
        folder=folder, poll_timeout_s=poll_timeout_s,
    )
