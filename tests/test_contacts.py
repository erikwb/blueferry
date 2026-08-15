"""Tests for extracting messaging addresses from PBAP vCards."""
from __future__ import annotations

import textwrap
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from blueferry import config, contact_repository, contacts
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


def test_phonebook_transfer_uses_runtime_dir_and_cleans_up_on_failure(
    tmp_path, monkeypatch
) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    target = None

    class _Pbap:
        def Select(self, *_args, **_kwargs):
            pass

        def PullAll(self, path, *_args, **_kwargs):
            nonlocal target
            target = Path(path)
            raise RuntimeError("transfer setup failed")

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setattr(contacts, "obex", lambda *_args: _Pbap())

    with pytest.raises(RuntimeError, match="transfer setup failed"):
        contacts.pull_phonebook(SimpleNamespace(pbap_path="/pbap"))

    assert target is not None
    assert target.parent.parent == runtime_dir / "blueferry"
    assert not target.parent.exists()
    assert (runtime_dir / "blueferry").stat().st_mode & 0o777 == 0o700


def test_phonebook_transfer_fails_closed_without_runtime_dir(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "persistent-state")

    with pytest.raises(RuntimeError, match="requires XDG_RUNTIME_DIR"):
        contacts.pull_phonebook(SimpleNamespace(pbap_path="/pbap"))

    assert not config.STATE_DIR.exists()


def test_phonebook_transfer_wires_idle_and_overall_timeouts(
    tmp_path, monkeypatch
) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    captured = {}

    class _Pbap:
        def Select(self, *_args, **_kwargs):
            pass

        def PullAll(self, *_args, **_kwargs):
            return "/transfer/phonebook", {"Status": "active", "Size": 0}

    def capture_wait(transfer_path, **kwargs):
        captured["transfer_path"] = transfer_path
        captured.update(kwargs)
        raise RuntimeError("stop after timeout wiring")

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setattr(contacts, "obex", lambda *_args: _Pbap())
    monkeypatch.setattr(contacts, "wait_for_transfer", capture_wait)

    with pytest.raises(RuntimeError, match="stop after timeout wiring"):
        contacts.pull_phonebook(SimpleNamespace(pbap_path="/pbap"))

    assert captured["transfer_path"] == "/transfer/phonebook"
    assert captured["timeout_s"] == 60
    assert captured["overall_timeout_s"] == 30 * 60
    assert callable(captured["get_progress"])


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
    with closing(contact_repository._open_db()) as database:
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


def test_records_page_by_display_name_without_splitting_a_person(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "CONTACTS_DB", tmp_path / "contacts.sqlite")
    monkeypatch.setattr(config, "EVENTS_DB", tmp_path / "events.sqlite")
    with closing(contact_repository._open_db()) as database:
        with database:
            for name, phone in (("Zoe Last", "15550000002"),
                                ("Alice Example", "15551234567")):
                cursor = database.execute(
                    "INSERT INTO contacts(full_name, updated_at) VALUES (?, ?)",
                    (name, 0),
                )
                database.execute(
                    "INSERT INTO phones(phone_norm, contact_id) VALUES (?, ?)",
                    (phone, cursor.lastrowid),
                )
            database.execute(
                "INSERT INTO emails(email, contact_id)"
                " VALUES (?, (SELECT id FROM contacts WHERE full_name = ?))",
                ("alice@example.com", "Alice Example"),
            )

    resolver = ContactsResolver()

    assert resolver.records() == [
        ("Alice Example", ["15551234567"], ["alice@example.com"]),
        ("Zoe Last", ["15550000002"], []),
    ]
    assert resolver.records(0, 1) == [
        ("Alice Example", ["15551234567"], ["alice@example.com"]),
    ]
    assert resolver.records(1, 1) == [("Zoe Last", ["15550000002"], [])]
    assert resolver.records(5, 1) == []


def test_records_tolerate_a_malformed_stored_row(tmp_path, monkeypatch):
    """A partially written row must not take the whole phonebook down."""
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "CONTACTS_DB", tmp_path / "contacts.sqlite")
    monkeypatch.setattr(config, "EVENTS_DB", tmp_path / "events.sqlite")

    resolver = ContactsResolver.__new__(ContactsResolver)
    resolver.storage = None
    resolver._repository = SimpleNamespace(load=lambda: [
        ("Alice Example", None, ["alice@example.com"]),
        ("Bob Other", "5551234567", None),
        (None, ["15551112222"], []),
    ])
    resolver._mem = {}
    resolver._records = []
    resolver._warm()

    assert resolver.records() == [
        (None, ["15551112222"], []),
        ("Alice Example", [], ["alice@example.com"]),
        ("Bob Other", [], []),
    ]


def test_records_are_ordered_once_at_load(tmp_path, monkeypatch):
    """Paging slices an already-sorted cache rather than re-sorting."""
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "CONTACTS_DB", tmp_path / "contacts.sqlite")
    monkeypatch.setattr(config, "EVENTS_DB", tmp_path / "events.sqlite")

    resolver = ContactsResolver.__new__(ContactsResolver)
    resolver.storage = None
    resolver._repository = SimpleNamespace(load=lambda: [
        ("Zoe Last", ["15550000002"], []),
        ("Alice Example", ["15551234567"], []),
    ])
    resolver._mem = {}
    resolver._records = []
    resolver._warm()

    assert [record[0] for record in resolver._records] == [
        "Alice Example", "Zoe Last",
    ]
    assert resolver.records(0, 1) is not resolver._records
    assert resolver.records(0, 1) == [("Alice Example", ["15551234567"], [])]


def test_resolver_only_equates_nanp_country_code_variants() -> None:
    resolver = ContactsResolver.__new__(ContactsResolver)
    resolver._mem = {"15551234567": {"Alice"}}

    assert resolver.resolve("5551234567") == "Alice"
    assert resolver.resolve("+1 555 123 4567") == "Alice"
    assert resolver.resolve("+44 1 555 123 4567") is None


def test_resolver_rejects_ambiguous_contact_names() -> None:
    resolver = ContactsResolver.__new__(ContactsResolver)
    resolver._mem = {"15551234567": {"Alice", "Other Alice"}}

    assert resolver.resolve("15551234567") is None
    assert resolver.resolve("5551234567") is None
