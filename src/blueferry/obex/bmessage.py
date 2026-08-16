"""Minimal bMessage parser.

The Bluetooth MAP spec wraps each SMS in a 'bMessage' container:

    BEGIN:BMSG
    VERSION:1.0
    STATUS:UNREAD
    TYPE:SMS_GSM
    FOLDER:telecom/msg/inbox
    BEGIN:VCARD                ← originator
    VERSION:2.1
    N:Smith;John;;;
    TEL:+15551234567
    END:VCARD
    BEGIN:BENV
    BEGIN:BBODY
    CHARSET:UTF-8
    LENGTH:21
    BEGIN:MSG
    Hello from my phone
    END:MSG
    END:BBODY
    END:BENV
    END:BMSG

We extract the sender address (TEL or EMAIL from the first VCARD) + body
(between BEGIN:MSG / END:MSG) and ignore everything else.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from blueferry.limits import (
    MAX_CONTACT_ADDRESS_CHARS,
    MAX_CONTACT_NAME_CHARS,
    MAX_REMOTE_PROPERTY_CHARS,
)
from blueferry.vcard import iter_vcard_bodies

_BMSG_STATUS_RE = re.compile(r"^STATUS:(?P<v>\S+)", re.MULTILINE | re.IGNORECASE)
_BMSG_TYPE_RE   = re.compile(r"^TYPE:(?P<v>\S+)",   re.MULTILINE | re.IGNORECASE)
_BMSG_FOLDER_RE = re.compile(r"^FOLDER:(?P<v>\S+)", re.MULTILINE | re.IGNORECASE)


@dataclass(slots=True)
class ParsedBMessage:
    sender_address: str | None
    sender_name: str | None
    body: str | None
    status: str | None       # "READ" / "UNREAD"
    type: str | None         # "SMS_GSM" etc.
    folder: str | None


def _originator_region(blob: str) -> str:
    """Return only the bMessage header preceding the first envelope."""
    offset = 0
    for line in blob.splitlines(keepends=True):
        if line.strip().casefold() == "begin:benv":
            return blob[:offset]
        offset += len(line)
    return blob


def _message_body(blob: str) -> str | None:
    """Extract the first complete MSG block in one forward scan.

    MAP permits indentation around structural markers. Track the first open
    marker for each indentation and choose the earliest one that has a matching
    terminator. This recovers from malformed prefixes without rescanning them.
    """
    candidates: dict[str, tuple[int, int]] = {}
    matches: list[tuple[int, int, int]] = []
    cursor = 0
    length = len(blob)

    while cursor < length:
        line_start = cursor
        newline = blob.find("\n", cursor)
        if newline < 0:
            raw_line = blob[cursor:]
            cursor = length
            terminated = False
        else:
            raw_line = blob[cursor:newline]
            cursor = newline + 1
            terminated = True
        line = raw_line[:-1] if raw_line.endswith("\r") else raw_line
        content = line.lstrip(" \t")
        indent = line[:len(line) - len(content)]
        marker = content.rstrip(" \t").casefold()

        if marker == "begin:msg" and terminated:
            candidates.setdefault(indent, (line_start, cursor))
        elif marker == "end:msg" and indent in candidates:
            begin_start, body_start = candidates.pop(indent)
            matches.append((begin_start, body_start, line_start))

    if not matches:
        return None
    _begin_start, body_start, body_end = min(matches, key=lambda item: item[0])
    return blob[body_start:body_end]


def parse(blob: str) -> ParsedBMessage:
    # Only a VCARD before BENV is an originator. Cards in the envelope are
    # recipients, while card-shaped text in BEGIN:MSG is untrusted body data.
    phone_address: str | None = None
    sender_email: str | None = None
    sender_name: str | None = None
    vc = next(iter_vcard_bodies(_originator_region(blob), maximum=1), None)
    if vc is not None:
        for line in vc.splitlines():
            line = line.strip()
            if not line:
                continue
            up = line.upper()
            if up.startswith("FN:"):
                sender_name = line[3:].strip() or None
            elif up.startswith("N:") and sender_name is None:
                # N: surname; first; middle; prefix; suffix
                parts = line[2:].split(";")
                joined = " ".join(p.strip() for p in (parts[1:2] + parts[0:1])
                                  if p.strip())
                sender_name = joined or None
            elif up.startswith("TEL"):
                _, _, val = line.partition(":")
                candidate = val.strip()
                if (
                    candidate
                    and len(candidate) <= MAX_CONTACT_ADDRESS_CHARS
                    and phone_address is None
                ):
                    phone_address = candidate
            elif up.startswith("EMAIL"):
                _, _, val = line.partition(":")
                candidate = val.strip()
                if (
                    candidate
                    and len(candidate) <= MAX_CONTACT_ADDRESS_CHARS
                    and sender_email is None
                ):
                    sender_email = candidate

    # A TEL value is the primary MAP originator when both fields are present;
    # EMAIL covers Apple-ID-only iMessage peers.
    sender_address = phone_address or sender_email

    raw_body = _message_body(blob)
    body = raw_body.strip("\r\n ") if raw_body is not None else None
    # Strip MAP byte-stuffing (the leading space convention for lines that
    # would otherwise start with `END:`).
    if body:
        body = "\n".join(
            line[1:]
            if line.startswith(" ")
            and line[1:].upper().startswith(("BEGIN:", "END:"))
            else line
            for line in body.splitlines()
        )

    def _first(rx) -> str | None:
        mm = rx.search(blob)
        return mm.group("v") if mm else None

    return ParsedBMessage(
        sender_address=sender_address,
        sender_name=(sender_name[:MAX_CONTACT_NAME_CHARS] if sender_name else None),
        body=body,
        status=(value[:MAX_REMOTE_PROPERTY_CHARS]
                if (value := _first(_BMSG_STATUS_RE)) else None),
        type=(value[:MAX_REMOTE_PROPERTY_CHARS]
              if (value := _first(_BMSG_TYPE_RE)) else None),
        folder=(value[:MAX_REMOTE_PROPERTY_CHARS]
                if (value := _first(_BMSG_FOLDER_RE)) else None),
    )
