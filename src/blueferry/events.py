"""Normalized event types emitted by the OBEX layer.

The iPhone's MAP server speaks bMessages and proprietary metadata;
the rest of the daemon shouldn't have to care. Everything upstream
sees these simple dataclasses.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

# ---- helpers ------------------------------------------------------------

_PHONE_KEEP = re.compile(r"\D")

# A sender string we're willing to show verbatim: digits and the punctuation
# a phone number legitimately carries. Anything else (letters, markup) is a
# name the *message* supplied, not one we resolved, so we don't display it.
_PHONE_SHAPED = re.compile(r"^[+0-9(][0-9 ()\-.+]*$")
_EMAIL_SHAPED = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$"
)

def normalize_phone(raw: str | None) -> str | None:
    """Reduce a phone string to digits only (E.164-ish minus the +).

    "+1 (561) 235-1044" → "15551234567"
    "5612351044"        → "5612351044"
    "Mom"               → None  (looked like a name)
    """
    if not raw or not _PHONE_SHAPED.fullmatch(raw.strip()):
        return None
    digits = _PHONE_KEEP.sub("", raw)
    # 7+ digits is plausibly a phone number
    return digits if len(digits) >= 7 else None


def is_phone_shaped(raw: str | None) -> bool:
    """True if `raw` is safe to show as a phone number.

    Sender strings arrive from the message itself, so anything containing
    letters or markup is a name the sender chose, not one we resolved.
    """
    return bool(raw and _PHONE_SHAPED.match(raw.strip()))


def is_email_shaped(raw: str | None) -> bool:
    """True if `raw` is safe to display as an email-style identifier."""
    return bool(raw and _EMAIL_SHAPED.fullmatch(raw.strip()))


def canonical_address(raw: str | None) -> str | None:
    """Return a stable, typed identity for a safe messaging address.

    Display names are deliberately never accepted here.  Thread IDs and
    group IDs use this value so two contacts named alike cannot be merged,
    and formatting differences in a phone number cannot split one thread.
    """
    value = (raw or "").strip()
    phone = normalize_phone(value)
    if phone:
        return f"phone:{phone}"
    if is_email_shaped(value):
        return f"email:{value.casefold()}"
    return None


def safe_event_address(ev: dict) -> str | None:
    """Extract a reply-safe address from a serialized message event."""
    raw = str(ev.get("sender_address") or "").strip()
    if canonical_address(raw):
        return raw
    normalized = str(ev.get("sender_phone_norm") or "").strip()
    return normalized if canonical_address(normalized) else None


def display_sender_from_dict(ev: dict) -> str:
    """`SmsEvent.display_sender` for an already-serialized event.

    Backend history APIs expose serialized events as plain dicts, so they
    need the same trust rule the live event objects apply — otherwise a
    spoofed `TEL:` reappears as a thread name on the next app start.
    """
    group = ev.get("group_name")
    if group:
        return str(group)
    contact = ev.get("contact_name")
    if contact:
        return str(contact)
    raw = str(ev.get("sender_address") or "").strip()
    if is_phone_shaped(raw) or is_email_shaped(raw):
        return raw
    return str(ev.get("sender_phone_norm") or "") or "(unknown)"


def parse_map_timestamp(ts: str | None) -> datetime | None:
    """Parse MAP's timestamp format: '20260519T181423' or with timezone suffix."""
    if not ts:
        return None
    try:
        fmt = "%Y%m%dT%H%M%S" + ("%z" if len(ts) > 15 else "")
        dt = datetime.strptime(ts, fmt)
        # Offset-free MAP dates use local time on the date of the message.
        return dt if dt.tzinfo is not None else dt.astimezone()
    except (ValueError, OverflowError, OSError):
        return None


# ---- event types --------------------------------------------------------

EventKind = Literal["sms_received", "sms_seen", "sms_sent"]


@dataclass(slots=True)
class SmsEvent:
    """A single SMS message event from the iPhone via MAP."""

    kind: EventKind
    handle: str                 # BlueZ obex Message1 path tail, e.g. "message93446842893444124"
    sender_address: str | None  # raw TEL or EMAIL address from MAP
    sender_phone_norm: str | None  # digits-only, for contacts lookup
    contact_name: str | None    # resolved from contacts cache, may be None
    body: str | None            # MAP puts the SMS text in `Subject`
    timestamp: datetime | None
    is_read: bool
    # Full BlueZ obex DBus path to the Message1 object, so downstream
    # code (e.g. libnotify sink) can write back read-state.
    message_path: str | None = None
    # Populated when a MAP message is correlated with its Messages ANCS
    # notification.  MAP itself carries none of this group context.
    group_key: str | None = None
    group_name: str | None = None
    group_members: list[str] = field(default_factory=list)
    group_recipients: list[str] = field(default_factory=list)
    group_reply_ready: bool = False
    seen_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    @property
    def display_sender(self) -> str:
        """Best *trustworthy* name for the sender.

        Only three things are ever shown: a name resolved from the local
        contacts cache, a phone number, or an email-shaped Messages address.
        `sender_address` is the raw address out of the inbound bMessage, so a
        sender can put arbitrary text there — `TEL:Mom` would otherwise render
        as a notification from Mom. Anything without a safe address shape
        falls through to the normalized digits, or to "(unknown)".
        """
        if self.group_name:
            return self.group_name
        if self.contact_name:
            return self.contact_name
        raw = (self.sender_address or "").strip()
        if is_phone_shaped(raw) or is_email_shaped(raw):
            return raw
        return self.sender_phone_norm or "(unknown)"

    def to_dict(self) -> dict:
        """Serializable form for persistent history and D-Bus."""
        return {
            "kind": self.kind,
            "handle": self.handle,
            "sender_address": self.sender_address,
            "sender_phone_norm": self.sender_phone_norm,
            "contact_name": self.contact_name,
            "body": self.body,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "is_read": self.is_read,
            "group_key": self.group_key,
            "group_name": self.group_name,
            "group_members": list(self.group_members),
            "group_recipients": list(self.group_recipients),
            "group_reply_ready": self.group_reply_ready,
            "seen_at": self.seen_at.isoformat(),
        }


def sms_sent_event(
    recipient: str,
    body: str,
    *,
    contact_name: str | None = None,
    transfer_path: str = "",
) -> SmsEvent:
    """Build an SmsEvent for a message *we* just sent via MAP PushMessage.

    For a sent message the relevant party is the recipient, so the
    `sender_*` / `contact_name` fields carry the recipient — that keeps it
    in the same conversation thread as incoming messages from that person.
    """
    handle = (transfer_path.rsplit("/", 1)[-1] if transfer_path
              else f"sent-{datetime.now(timezone.utc):%Y%m%d%H%M%S%f}")
    return SmsEvent(
        kind="sms_sent",
        handle=handle,
        sender_address=recipient,
        sender_phone_norm=normalize_phone(recipient),
        contact_name=contact_name,
        body=body,
        timestamp=datetime.now().astimezone(),
        is_read=True,
        message_path=None,
    )


def sms_group_sent_event(
    recipients: list[str],
    body: str,
    *,
    group_key: str,
    group_name: str,
    group_members: list[str],
    transfer_path: str = "",
) -> SmsEvent:
    """Build the local history event for one verified group MAP send."""
    if len(recipients) < 2:
        raise ValueError("a group sent event requires at least two recipients")
    event = sms_sent_event(
        recipients[0], body,
        contact_name=None,
        transfer_path=transfer_path,
    )
    event.group_key = group_key
    event.group_name = group_name
    event.group_members = list(group_members)
    event.group_recipients = list(recipients)
    event.group_reply_ready = True
    return event
