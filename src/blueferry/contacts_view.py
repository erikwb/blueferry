"""Toolkit-free rows and wording for the contact view, shared by every client.

Input is ``Messages1.ListContactCards(offset, limit, query)``::

    {"contacts": [{"id", "name", "organization", "primary", "phone_count",
                   "email_count", "address_count", "has_photo"}],
     "total", "offset", "query"}

and ``Messages1.GetContact(id)``, a JSON document plus photo bytes::

    {"id", "name", "organization", "title",
     "phones": [{"kind", "label", "value", "number"}],
     "emails": [{"kind", "label", "value"}],
     "addresses": [{"kind", "label", "po_box", "extended", "street",
                    "locality", "region", "postal_code", "country"}],
     "has_photo", "photo_retry"}

Ids are valid while a card is unchanged within one daemon run; GetContact
answers NotFound for a stale id, and the client reloads the list. A list
row never carries photo bytes: a client fetches the photo with GetContact
when it opens a card and the photos of the rows on screen with
GetContactCardPhotos (see contacts_model.ContactBook.request_photos).

Every string returned here is plain text taken verbatim from the iPhone or
from this module; each client escapes it for its own renderer.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from blueferry.i18n import _, ngettext

STATUS_KEY = "contact_card_count"
# Changes on every daemon restart and contact reload: reload the list and
# drop held card ids when it differs from the value they were fetched under.
EPOCH_KEY = "contact_card_epoch"
DEFAULT_PAGE = 100
POSTAL_FIELDS = (
    "po_box", "extended", "street", "locality", "region", "postal_code", "country",
)

# Section identifiers of detail_sections(); clients may pick icons by them.
SECTION_PHONES = "phones"
SECTION_EMAILS = "emails"
SECTION_ADDRESSES = "addresses"

# Row actions: "dial" (number), "message" (number), "mail" (address),
# "copy" (text). Clients offer what they support.
ACTION_DIAL = "dial"
ACTION_MESSAGE = "message"
ACTION_MAIL = "mail"
ACTION_COPY = "copy"


def supported(status: Mapping[str, Any]) -> bool:
    """Whether the running daemon offers contact cards (GetStatus capability key)."""
    value = status.get(STATUS_KEY)
    return isinstance(value, int) and not isinstance(value, bool)


def photos_shown(status: Mapping[str, Any]) -> bool:
    """Whether contact photos may be shown: on, and the saved choice is known."""
    return (
        status.get("contact_photos") is True
        and status.get("contact_photos_state") != "unknown"
    )


def _decode(payload: object) -> Mapping[str, Any]:
    if isinstance(payload, str | bytes):
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = {}
    return payload if isinstance(payload, Mapping) else {}


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def normalize_page(payload: object) -> dict[str, Any]:
    """A ListContactCards page with every key present and malformed rows dropped."""
    value = _decode(payload)
    contacts: list[dict[str, Any]] = []
    seen: set[str] = set()
    raw = value.get("contacts")
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, Mapping):
            continue
        card_id = item.get("id")
        if not isinstance(card_id, str) or not card_id or card_id in seen:
            continue
        seen.add(card_id)
        contacts.append({
            "id": card_id,
            "name": _text(item.get("name")),
            "organization": _text(item.get("organization")),
            "primary": _text(item.get("primary")),
            "phone_count": _count(item.get("phone_count")),
            "email_count": _count(item.get("email_count")),
            "address_count": _count(item.get("address_count")),
            "has_photo": item.get("has_photo") is True,
        })
    return {
        "contacts": contacts,
        "total": max(_count(value.get("total")), len(contacts)),
        "offset": _count(value.get("offset")),
        "query": _text(value.get("query")),
    }


def _labelled_rows(raw: object, fields: tuple[str, ...]) -> list[dict[str, str]]:
    rows = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, Mapping):
            rows.append({field: _text(item.get(field)) for field in fields})
    return rows


def normalize_card(payload: object) -> dict[str, Any]:
    """A GetContact document with every key present."""
    value = _decode(payload)
    phones = [
        row for row in _labelled_rows(value.get("phones"), ("kind", "label", "value", "number"))
        if row["value"]
    ]
    emails = [
        row for row in _labelled_rows(value.get("emails"), ("kind", "label", "value"))
        if row["value"]
    ]
    addresses = [
        row for row in _labelled_rows(value.get("addresses"), ("kind", "label", *POSTAL_FIELDS))
        if any(row[field] for field in POSTAL_FIELDS)
    ]
    return {
        "id": _text(value.get("id")),
        "name": _text(value.get("name")),
        "organization": _text(value.get("organization")),
        "title": _text(value.get("title")),
        "phones": phones,
        "emails": emails,
        "addresses": addresses,
        "has_photo": value.get("has_photo") is True,
        "photo_retry": value.get("photo_retry") is True,
    }


def kind_label(kind: str, label: str = "") -> str:
    """Display text for a phone, email or address label kind."""
    if label:
        return label
    labels = {
        "mobile": _("Mobile"),
        "iphone": _("iPhone"),
        "home": _("Home"),
        "work": _("Work"),
        "main": _("Main"),
        "home_fax": _("Home fax"),
        "work_fax": _("Work fax"),
        "other_fax": _("Fax"),
        "pager": _("Pager"),
        "other": _("Other"),
    }
    return labels.get(kind, _("Other"))


def display_name(card: Mapping[str, Any]) -> str:
    """The card's name, else its organization, else its first number or address."""
    for key in ("name", "organization", "primary"):
        value = _text(card.get(key)).strip()
        if value:
            return value
    for key in ("phones", "emails"):
        rows = card.get(key)
        if isinstance(rows, list) and rows and isinstance(rows[0], Mapping):
            value = _text(rows[0].get("value")).strip()
            if value:
                return value
    return _("No name")


def initials(name: str) -> str:
    """Up to two initials for an avatar placeholder; "" for no letters."""
    letters = [word[0] for word in name.split() if word[:1].isalpha()]
    if not letters:
        return ""
    return (letters[0] + (letters[-1] if len(letters) > 1 else "")).upper()


def list_rows(payload: object) -> list[dict[str, Any]]:
    """One row per card in the daemon's order (display-name order).

    Each row: ``id``, ``title``, ``subtitle`` (organization and first
    number or address), ``initials`` and ``has_photo``.
    """
    rows = []
    for card in normalize_page(payload)["contacts"]:
        title = display_name(card)
        details = [
            value for value in (card["organization"], card["primary"])
            if value and value != title
        ]
        rows.append({
            "id": card["id"],
            "title": title,
            "subtitle": " · ".join(details),
            "initials": initials(card["name"] or card["organization"]),
            "has_photo": card["has_photo"],
        })
    return rows


def has_more(payload: object) -> bool:
    page = normalize_page(payload)
    return page["offset"] + len(page["contacts"]) < page["total"]


def next_offset(payload: object) -> int:
    page = normalize_page(payload)
    return page["offset"] + len(page["contacts"])


def count_text(payload: object) -> str:
    page = normalize_page(payload)
    total = page["total"]
    if page["query"]:
        text = ngettext("{count} matching contact", "{count} matching contacts", total)
    else:
        text = ngettext("{count} contact", "{count} contacts", total)
    return text.format(count=total)


def empty_hint(payload: object) -> str:
    """Why the list is empty; "" when it is not."""
    page = normalize_page(payload)
    if page["contacts"] or page["total"]:
        return ""
    if page["query"]:
        return _("No contact matches “{query}”.").format(query=page["query"])
    return _(
        "No contacts are cached yet. Turn on Sync Contacts for this computer in "
        "the iPhone's Bluetooth settings, then sync contacts."
    )


def format_address(address: Mapping[str, Any]) -> str:
    """A postal address as lines: street parts, postal code and town, region, country."""
    def part(name: str) -> str:
        return _text(address.get(name)).strip()

    town = " ".join(value for value in (part("postal_code"), part("locality")) if value)
    lines = [part("po_box"), part("extended"), part("street"), town, part("region"),
             part("country")]
    return "\n".join(line for line in lines if line)


def detail_sections(payload: object) -> list[dict[str, Any]]:
    """The card's numbers, email and postal addresses as labelled sections.

    Each section: ``id`` (SECTION_*), ``title`` and ``rows``; each row:
    ``label``, ``value`` (display text), ``target`` (what an action uses)
    and ``actions`` (ACTION_* names in order of preference).
    """
    card = normalize_card(payload)
    sections = []
    phones = [
        {
            "label": kind_label(row["kind"], row["label"]),
            "value": row["value"],
            "target": row["number"] or row["value"],
            "actions": (
                [ACTION_DIAL, ACTION_MESSAGE, ACTION_COPY] if row["number"]
                else [ACTION_COPY]
            ),
        }
        for row in card["phones"]
    ]
    if phones:
        sections.append({"id": SECTION_PHONES, "title": _("Phone"), "rows": phones})
    emails = [
        {
            "label": kind_label(row["kind"], row["label"]),
            "value": row["value"],
            "target": row["value"],
            "actions": [ACTION_MAIL, ACTION_MESSAGE, ACTION_COPY],
        }
        for row in card["emails"]
    ]
    if emails:
        sections.append({"id": SECTION_EMAILS, "title": _("Email"), "rows": emails})
    addresses = []
    for row in card["addresses"]:
        text = format_address(row)
        addresses.append({
            "label": kind_label(row["kind"], row["label"]),
            "value": text,
            "target": text,
            "actions": [ACTION_COPY],
        })
    if addresses:
        sections.append({"id": SECTION_ADDRESSES, "title": _("Address"), "rows": addresses})
    return sections


def card_header(payload: object) -> dict[str, Any]:
    """Title, subtitle (job title and organization) and avatar hints of a card."""
    card = normalize_card(payload)
    title = display_name(card)
    subtitle = " · ".join(
        value for value in (card["title"], card["organization"]) if value and value != title
    )
    return {
        "title": title,
        "subtitle": subtitle,
        "initials": initials(card["name"] or card["organization"]),
        "has_photo": card["has_photo"],
        "photo_retry": card["photo_retry"],
    }


def list_view(payload: object) -> dict[str, Any]:
    """One list page for a client, e.g. the Quickshell bridge."""
    rows = list_rows(payload)
    return {
        "rows": rows,
        "count": len(rows),
        "count_text": count_text(payload),
        "hint": empty_hint(payload),
        "has_more": has_more(payload),
        "next_offset": next_offset(payload),
    }


def card_view(payload: object) -> dict[str, Any]:
    """The whole card for a client: header plus detail sections."""
    return {**card_header(payload), "sections": detail_sections(payload)}


def search_placeholder() -> str:
    return _("Search contacts")


def stale_text() -> str:
    """Shown when GetContact answers NotFound: the list must be reloaded."""
    return _("This contact changed or was removed. Reload the list.")


def load_failed_text(error: object) -> str:
    detail = str(error).strip()
    return detail or _("Could not load contacts.")
