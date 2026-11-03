"""Contact-card details from PBAP vCards: labelled numbers, mail, addresses.

:mod:`blueferry.contacts` keeps the messaging view of a card (name plus the
normalized phone and email identities used for routing). This module keeps
what a contact *view* needs on top of that: every number as the iPhone
stores it with its label, labelled email addresses, postal addresses, the
organization and the job title.

The details are stored inside the sealed contact payload (purpose
``contact-record-v1``) and never in a plaintext table. Older versions ignore
the extra key, so a downgrade keeps working and drops the details at the
next sync.
"""
from __future__ import annotations

import codecs
import quopri
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from blueferry import config
from blueferry.events import is_email_shaped
from blueferry.limits import (
    MAX_CONTACT_ADDRESS_CHARS,
    MAX_CONTACT_ADDRESSES_PER_CARD,
    MAX_CONTACT_DETAIL_CHARS,
    MAX_CONTACT_LABEL_CHARS,
    MAX_CONTACT_POSTAL_ADDRESSES_PER_CARD,
)
from blueferry.phone_numbers import contact_phone

# Machine-readable label kinds. Clients translate them; ``custom`` carries
# the user's own label text in ``label``.
PHONE_KINDS = frozenset({
    "mobile", "iphone", "home", "work", "main", "home_fax", "work_fax",
    "other_fax", "pager", "other", "custom",
})
EMAIL_KINDS = frozenset({"home", "work", "other", "custom"})
ADDRESS_KINDS = frozenset({"home", "work", "other", "custom"})
POSTAL_FIELDS = (
    "po_box", "extended", "street", "locality", "region", "postal_code", "country",
)

# Apple's built-in X-ABLabel values (``_$!<Mobile>!$_``) for grouped items.
_APPLE_LABELS = {
    "MOBILE": "mobile", "HOME": "home", "WORK": "work", "MAIN": "main",
    "HOMEFAX": "home_fax", "WORKFAX": "work_fax", "OTHERFAX": "other_fax",
    "PAGER": "pager", "OTHER": "other", "IPHONE": "iphone",
}


@dataclass(frozen=True, slots=True)
class LabeledValue:
    """One number or email address with its label.

    ``number`` is the normalized phone spelling messaging and dialling use
    (empty for email and for numbers that cannot be normalized).
    """

    kind: str
    label: str
    value: str
    number: str = ""


@dataclass(frozen=True, slots=True)
class PostalAddress:
    kind: str
    label: str
    po_box: str = ""
    extended: str = ""
    street: str = ""
    locality: str = ""
    region: str = ""
    postal_code: str = ""
    country: str = ""

    def fields(self) -> dict[str, str]:
        return {name: getattr(self, name) for name in POSTAL_FIELDS}


@dataclass(frozen=True, slots=True)
class ContactDetails:
    phones: tuple[LabeledValue, ...] = ()
    emails: tuple[LabeledValue, ...] = ()
    addresses: tuple[PostalAddress, ...] = ()
    organization: str = ""
    title: str = ""

    def __bool__(self) -> bool:
        return bool(
            self.phones or self.emails or self.addresses
            or self.organization or self.title
        )

    def to_storage(self) -> dict[str, Any]:
        """Compact JSON-ready form for the sealed contact payload."""
        value: dict[str, Any] = {}
        if self.phones:
            value["phones"] = [
                [item.kind, item.label, item.value, item.number] for item in self.phones
            ]
        if self.emails:
            value["emails"] = [[item.kind, item.label, item.value] for item in self.emails]
        if self.addresses:
            value["addresses"] = [
                [item.kind, item.label, *(getattr(item, name) for name in POSTAL_FIELDS)]
                for item in self.addresses
            ]
        if self.organization:
            value["organization"] = self.organization
        if self.title:
            value["title"] = self.title
        return value

    @classmethod
    def from_storage(cls, value: object) -> ContactDetails:
        """Rebuild from decrypted JSON; malformed parts are dropped, not raised."""
        if not isinstance(value, Mapping):
            return EMPTY
        phones = []
        for item in _rows(value.get("phones"), MAX_CONTACT_ADDRESSES_PER_CARD):
            if len(item) != 4:
                continue
            kind, label, text, number = item
            if kind not in PHONE_KINDS or not text or len(text) > MAX_CONTACT_ADDRESS_CHARS:
                continue
            phones.append(LabeledValue(
                kind, label[:MAX_CONTACT_LABEL_CHARS], text,
                number if len(number) <= MAX_CONTACT_ADDRESS_CHARS else "",
            ))
        emails = []
        for item in _rows(value.get("emails"), MAX_CONTACT_ADDRESSES_PER_CARD):
            if len(item) != 3:
                continue
            kind, label, text = item
            if kind not in EMAIL_KINDS or len(text) > MAX_CONTACT_ADDRESS_CHARS or not (
                is_email_shaped(text)
            ):
                continue
            emails.append(LabeledValue(kind, label[:MAX_CONTACT_LABEL_CHARS], text))
        addresses = []
        for item in _rows(value.get("addresses"), MAX_CONTACT_POSTAL_ADDRESSES_PER_CARD):
            if len(item) != 2 + len(POSTAL_FIELDS) or item[0] not in ADDRESS_KINDS:
                continue
            parts = [part[:MAX_CONTACT_DETAIL_CHARS] for part in item[2:]]
            if any(parts):
                addresses.append(PostalAddress(
                    item[0], item[1][:MAX_CONTACT_LABEL_CHARS], *parts,
                ))
        return cls(
            tuple(phones), tuple(emails), tuple(addresses),
            _text(value.get("organization")), _text(value.get("title")),
        )


EMPTY = ContactDetails()


def _text(value: object) -> str:
    return value[:MAX_CONTACT_DETAIL_CHARS] if isinstance(value, str) else ""


def _rows(value: object, maximum: int) -> list[list[str]]:
    """Stored rows of strings only; anything else is skipped."""
    if not isinstance(value, list):
        return []
    rows = []
    for item in value[:maximum]:
        if isinstance(item, list) and all(isinstance(part, str) for part in item):
            rows.append(item)
    return rows


# ---- vCard parsing ----------------------------------------------------------

def _value_separator(line: str) -> int:
    """Index of the first ``:`` outside double-quoted parameter values."""
    quoted = False
    for index, character in enumerate(line):
        if character == '"':
            quoted = not quoted
        elif character == ":" and not quoted:
            return index
    return -1


def _split_escaped(value: str, separator: str = ";") -> list[str]:
    """Split at unescaped separators and resolve RFC 2426 text escapes."""
    parts: list[str] = []
    current: list[str] = []
    escaped = False
    for character in value:
        if escaped:
            current.append("\n" if character in "nN" else character)
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == separator:
            parts.append("".join(current))
            current = []
        else:
            current.append(character)
    parts.append("".join(current))
    return parts


def _unescape(value: str) -> str:
    return _split_escaped(value, separator="\0")[0]


@dataclass(frozen=True, slots=True)
class _Property:
    group: str
    name: str
    tokens: frozenset[str]
    value: str
    quoted_printable: bool = False
    charset: str = "utf-8"

    def decode(self, text: str) -> str:
        """Resolve a vCard 2.1 QUOTED-PRINTABLE value in its CHARSET.

        Applied per component, after splitting at unescaped ``;``, so an
        encoded ``=3B`` stays inside its component.
        """
        if not self.quoted_printable:
            return text
        return quopri.decodestring(text.encode("utf-8", "replace")).decode(
            self.charset, "replace",
        )


_QUOTED_PRINTABLE = "QUOTED-PRINTABLE"


def _charset(value: str) -> str:
    name = value.strip().strip('"')
    try:
        return codecs.lookup(name).name
    except LookupError:
        return "utf-8"


def _property(line: str) -> _Property | None:
    """Group, NAME, TYPE tokens, value and encoding of one unfolded content line."""
    separator = _value_separator(line)
    if separator < 0:
        return None
    head, value = line[:separator], line[separator + 1:]
    name, *params = head.split(";")
    group, _, name = name.strip().rpartition(".")
    tokens: set[str] = set()
    quoted_printable = False
    charset = "utf-8"
    for param in params:
        key, has_value, values = param.strip().partition("=")
        key = key.strip().upper()
        if has_value and key == "ENCODING":
            quoted_printable = values.strip().strip('"').upper() == _QUOTED_PRINTABLE
            continue
        if has_value and key == "CHARSET":
            charset = _charset(values)
            continue
        if has_value and key != "TYPE":
            continue
        for part in (values if has_value else key).split(","):
            word = part.strip().strip('"').upper()
            if word == _QUOTED_PRINTABLE:
                # vCard 2.1 allows the bare encoding name as a parameter.
                quoted_printable = True
            elif word:
                tokens.add(word)
    return _Property(
        group.upper(), name.upper(), frozenset(tokens), value, quoted_printable, charset,
    )


def _apple_label(value: str) -> tuple[str, str, str]:
    """``(kind, label, text)`` for an X-ABLabel value.

    ``text`` is the readable label (``Mobile`` for ``_$!<Mobile>!$_``), kept
    for a property whose kinds do not include the built-in one.
    """
    text = _unescape(value).strip()
    if text.startswith("_$!<") and text.endswith(">!$_"):
        text = text[4:-4]
        kind = _APPLE_LABELS.get(text.upper())
        if kind is not None:
            return kind, "", text[:MAX_CONTACT_LABEL_CHARS]
    text = text[:MAX_CONTACT_LABEL_CHARS]
    return "custom", text, text


def _phone_kind(tokens: frozenset[str]) -> str:
    if "FAX" in tokens:
        if "HOME" in tokens:
            return "home_fax"
        return "work_fax" if "WORK" in tokens else "other_fax"
    for token, kind in (
        ("PAGER", "pager"), ("IPHONE", "iphone"), ("CELL", "mobile"),
        ("MAIN", "main"), ("HOME", "home"), ("WORK", "work"),
    ):
        if token in tokens:
            return kind
    return "other"


def _place_kind(tokens: frozenset[str]) -> str:
    if "HOME" in tokens:
        return "home"
    return "work" if "WORK" in tokens else "other"


def _labelled(
    kind: str, group: str, labels: Mapping[str, tuple[str, str, str]], allowed: Iterable[str],
) -> tuple[str, str]:
    """An X-ABLabel on the same group overrides the TYPE parameters.

    A built-in Apple label that is no kind of this property (Mobile on an
    email address) becomes a custom label with its readable text.
    """
    if group and group in labels:
        label_kind, label, text = labels[group]
        if label_kind in allowed:
            return label_kind, label
        return "custom", text
    return kind, ""


def parse_card_details(body: str) -> ContactDetails:
    """The details of one unfolded vCard body (see ``iter_vcard_bodies``)."""
    lines: list[_Property] = []
    labels: dict[str, tuple[str, str, str]] = {}
    for raw in body.splitlines():
        parsed = _property(raw.strip())
        if parsed is None:
            continue
        if parsed.name == "X-ABLABEL" and parsed.group:
            labels[parsed.group] = _apple_label(parsed.decode(parsed.value))
        else:
            lines.append(parsed)
    phones: list[LabeledValue] = []
    emails: list[LabeledValue] = []
    addresses: list[PostalAddress] = []
    organization = title = ""
    for prop in lines:
        group, name, tokens = prop.group, prop.name, prop.tokens
        if name == "TEL" and len(phones) < MAX_CONTACT_ADDRESSES_PER_CARD:
            text = prop.decode(prop.value).strip()
            if not text or len(text) > MAX_CONTACT_ADDRESS_CHARS:
                continue
            kind, label = _labelled(_phone_kind(tokens), group, labels, PHONE_KINDS)
            number = contact_phone(text, config.PHONE_REGION) or ""
            phones.append(LabeledValue(kind, label, text, number))
        elif name == "EMAIL" and len(emails) < MAX_CONTACT_ADDRESSES_PER_CARD:
            text = prop.decode(prop.value).strip()
            if len(text) > MAX_CONTACT_ADDRESS_CHARS or not is_email_shaped(text):
                continue
            kind, label = _labelled(_place_kind(tokens), group, labels, EMAIL_KINDS)
            emails.append(LabeledValue(kind, label, text))
        elif name == "ADR" and len(addresses) < MAX_CONTACT_POSTAL_ADDRESSES_PER_CARD:
            parts = [
                prop.decode(part).strip()[:MAX_CONTACT_DETAIL_CHARS]
                for part in _split_escaped(prop.value)
            ]
            parts = (parts + [""] * len(POSTAL_FIELDS))[:len(POSTAL_FIELDS)]
            if not any(parts):
                continue
            kind, label = _labelled(_place_kind(tokens), group, labels, ADDRESS_KINDS)
            addresses.append(PostalAddress(kind, label, *parts))
        elif name == "ORG" and not organization:
            organization = ", ".join(
                text for text in (
                    prop.decode(part).strip() for part in _split_escaped(prop.value)
                ) if text
            )[:MAX_CONTACT_DETAIL_CHARS]
        elif name == "TITLE" and not title:
            title = prop.decode(_unescape(prop.value)).strip()[:MAX_CONTACT_DETAIL_CHARS]
    return ContactDetails(
        tuple(phones), tuple(emails), tuple(addresses), organization, title,
    )


# ---- D-Bus documents ----------------------------------------------------------

def _phone_rows(record_phones: Iterable[str], details: ContactDetails) -> list[dict[str, str]]:
    if details.phones:
        return [
            {"kind": item.kind, "label": item.label, "value": item.value, "number": item.number}
            for item in details.phones
        ]
    # Cards cached without details (plaintext cache, or synced by an older
    # version): the normalized numbers are all there is.
    return [
        {"kind": "other", "label": "", "value": phone, "number": phone}
        for phone in record_phones
    ]


def _email_rows(record_emails: Iterable[str], details: ContactDetails) -> list[dict[str, str]]:
    if details.emails:
        return [
            {"kind": item.kind, "label": item.label, "value": item.value}
            for item in details.emails
        ]
    return [{"kind": "other", "label": "", "value": email} for email in record_emails]


def card_summary(
    card_id: str,
    name: str | None,
    phones: Iterable[str],
    emails: Iterable[str],
    details: ContactDetails,
    *,
    has_photo: bool,
) -> dict[str, Any]:
    """One ``ListContactCards`` row: enough for a list, no photo bytes."""
    phone_rows = _phone_rows(phones, details)
    email_rows = _email_rows(emails, details)
    primary = phone_rows[0]["value"] if phone_rows else (
        email_rows[0]["value"] if email_rows else ""
    )
    return {
        "id": card_id,
        "name": name or "",
        "organization": details.organization,
        "primary": primary,
        "phone_count": len(phone_rows),
        "email_count": len(email_rows),
        "address_count": len(details.addresses),
        "has_photo": has_photo,
    }


def card_document(
    card_id: str,
    name: str | None,
    phones: Iterable[str],
    emails: Iterable[str],
    details: ContactDetails,
    *,
    has_photo: bool,
    photo_retry: bool = False,
) -> dict[str, Any]:
    """The ``GetContact`` document; the photo travels separately as bytes."""
    return {
        "id": card_id,
        "name": name or "",
        "organization": details.organization,
        "title": details.title,
        "phones": _phone_rows(phones, details),
        "emails": _email_rows(emails, details),
        "addresses": [
            {"kind": item.kind, "label": item.label, **item.fields()}
            for item in details.addresses
        ],
        "has_photo": has_photo,
        "photo_retry": photo_retry,
    }
