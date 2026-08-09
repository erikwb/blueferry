"""Tests for extracting messaging addresses from PBAP vCards."""
from __future__ import annotations

import textwrap
from contextlib import closing

from blueferry import config, contacts
from blueferry.contacts import (
    ContactsResolver,
    _parse_vcard_records,
    _pbap_pull_filters,
)
from blueferry.limits import (
    MAX_CONTACT_ADDRESS_CHARS,
    MAX_CONTACT_ADDRESSES_PER_CARD,
    MAX_CONTACT_NAME_CHARS,
)


def test_pbap_filters_use_phonebook_access_names():
    filters = _pbap_pull_filters(123)
    assert set(filters) == {"MaxCount", "Format"}
    assert int(filters["MaxCount"]) == 123
    assert str(filters["Format"]) == "vcard30"


def test_single_vcard():
    blob = textwrap.dedent("""\
        BEGIN:VCARD
        VERSION:3.0
        FN:John Smith
        TEL:+15551234567
        END:VCARD
        """)
    cards = _parse_vcard_records(blob)
    assert len(cards) == 1
    name, phones, emails = cards[0]
    assert name == "John Smith"
    assert phones == ["15551234567"]
    assert emails == []


def test_multiple_vcards():
    blob = textwrap.dedent("""\
        BEGIN:VCARD
        VERSION:3.0
        FN:Alice
        TEL:+15551234567
        END:VCARD
        BEGIN:VCARD
        VERSION:3.0
        FN:Bob
        TEL:+15559876543
        END:VCARD
        """)
    cards = _parse_vcard_records(blob)
    assert len(cards) == 2
    assert {c[0] for c in cards} == {"Alice", "Bob"}


def test_multiple_phones_per_card():
    blob = textwrap.dedent("""\
        BEGIN:VCARD
        FN:Multi
        TEL;TYPE=CELL:+15551111111
        TEL;TYPE=WORK:+15552222222
        TEL;TYPE=HOME:+15553333333
        END:VCARD
        """)
    cards = _parse_vcard_records(blob)
    assert len(cards) == 1
    name, phones, emails = cards[0]
    assert name == "Multi"
    assert sorted(phones) == ["15551111111", "15552222222", "15553333333"]
    assert emails == []


def test_card_with_no_phone():
    blob = textwrap.dedent("""\
        BEGIN:VCARD
        FN:Name Only
        END:VCARD
        """)
    cards = _parse_vcard_records(blob)
    assert len(cards) == 1
    name, phones, emails = cards[0]
    assert name == "Name Only"
    assert phones == []
    assert emails == []


def test_card_with_no_name():
    blob = textwrap.dedent("""\
        BEGIN:VCARD
        TEL:+15551234567
        END:VCARD
        """)
    cards = _parse_vcard_records(blob)
    assert len(cards) == 1
    name, phones, emails = cards[0]
    assert name is None
    assert phones == ["15551234567"]
    assert emails == []


def test_empty_blob():
    assert _parse_vcard_records("") == []


def test_email_addresses_are_available_to_contact_sync() -> None:
    blob = """BEGIN:VCARD
VERSION:3.0
FN:Apple ID Friend
EMAIL;TYPE=INTERNET:Friend@icloud.com
EMAIL:not an address
END:VCARD
"""
    assert _parse_vcard_records(blob) == [
        ("Apple ID Friend", [], ["friend@icloud.com"])
    ]


def test_malformed_skipped():
    # Half-vcard at end is dropped (no END:VCARD)
    blob = textwrap.dedent("""\
        BEGIN:VCARD
        FN:Complete
        TEL:+15551234567
        END:VCARD
        BEGIN:VCARD
        FN:Truncated
        """)
    cards = _parse_vcard_records(blob)
    assert len(cards) == 1
    assert cards[0][0] == "Complete"


def test_unicode_names():
    blob = textwrap.dedent("""\
        BEGIN:VCARD
        FN:Mañuel Garçia
        TEL:+15551234567
        END:VCARD
        BEGIN:VCARD
        FN:Маша
        TEL:+15552222222
        END:VCARD
        """)
    cards = _parse_vcard_records(blob)
    assert "Mañuel Garçia" in [c[0] for c in cards]
    assert "Маша" in [c[0] for c in cards]


def test_remote_contact_fields_are_bounded_before_persistence() -> None:
    blob = (
        "BEGIN:VCARD\n"
        f"FN:{'N' * (MAX_CONTACT_NAME_CHARS + 100)}\n"
        f"EMAIL:{'a' * MAX_CONTACT_ADDRESS_CHARS}@example.com\n"
        f"TEL:{'1' * (MAX_CONTACT_ADDRESS_CHARS + 1)}\n"
        "END:VCARD\n"
    )

    name, phones, emails = _parse_vcard_records(blob)[0]

    assert len(name or "") == MAX_CONTACT_NAME_CHARS
    assert phones == []
    assert emails == []


def test_phonebook_card_and_address_counts_are_bounded() -> None:
    address_lines = "\n".join(
        f"TEL:+1555{index:08d}"
        for index in range(MAX_CONTACT_ADDRESSES_PER_CARD + 10)
    )
    card = f"BEGIN:VCARD\nFN:Bounded\n{address_lines}\nEND:VCARD\n"

    parsed = _parse_vcard_records(card * 3, maximum=2)

    assert len(parsed) == 2
    assert len(parsed[0][1]) == MAX_CONTACT_ADDRESSES_PER_CARD


def test_find_by_name_returns_phone_and_email_destinations(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "CONTACTS_DB", tmp_path / "contacts.sqlite")
    monkeypatch.setattr(config, "EVENTS_DB", tmp_path / "events.sqlite")
    with closing(contacts._open_db()) as database:
        with database:
            cursor = database.execute(
                "INSERT INTO contacts(full_name, updated_at) VALUES (?, ?)",
                ("Alice Example", 0),
            )
            contact_id = cursor.lastrowid
            database.execute(
                "INSERT INTO phones(phone_norm, contact_id) VALUES (?, ?)",
                ("15551234567", contact_id),
            )
            database.execute(
                "INSERT INTO emails(email, contact_id) VALUES (?, ?)",
                ("alice@example.com", contact_id),
            )

    assert ContactsResolver().find_by_name("alice") == [
        ("Alice Example", "15551234567"),
        ("Alice Example", "alice@example.com"),
    ]
