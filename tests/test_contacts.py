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
from blueferry.obex import transfer
from blueferry.vcard import iter_vcard_bodies


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


@pytest.mark.parametrize("advertised_size", [0, 17])
def test_oversized_phonebook_is_cancelled_before_cleanup(
    tmp_path, monkeypatch, advertised_size,
):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(contacts, "MAX_PHONEBOOK_BYTES", 16)
    target = None
    cancelled = []

    class _Pbap:
        def Select(self, *_args, **_kwargs):
            pass

        def PullAll(self, path, *_args, **_kwargs):
            nonlocal target
            target = Path(path)
            target.write_bytes(b"x" * (1 if advertised_size else 17))
            return "/transfer/phonebook", {"Status": "active", "Size": advertised_size}

        def Cancel(self, *, timeout):
            assert target.exists(), "cancel before removing the temporary directory"
            cancelled.append(timeout)

    interface = _Pbap()
    monkeypatch.setattr(contacts, "obex", lambda *_args: interface)
    monkeypatch.setattr(transfer, "obex", lambda *_args: interface)

    with pytest.raises(RuntimeError, match="safety limit"):
        contacts.pull_phonebook(SimpleNamespace(pbap_path="/pbap"))

    assert cancelled == [2.0]
    assert not target.parent.exists()


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


def test_oversized_or_unterminated_vcards_do_not_hide_later_contacts() -> None:
    blob = (
        ("BEGIN:VCARD\n" * 10_000)
        + "FN:" + ("x" * (1024 * 1024 + 1)) + "\nEND:VCARD\n"
        + "BEGIN:VCARD\nFN:Safe\nTEL:+15551234567\nEND:VCARD\n"
    )

    assert _parse_vcard_records(blob) == [
        ("Safe", ["15551234567"], []),
    ]


def _folded(text: str, width: int = 75) -> str:
    return "\n ".join(text[i:i + width] for i in range(0, len(text), width))


def test_large_contact_photo_does_not_discard_the_card() -> None:
    # An 800 KiB JPEG is ~1.07 MiB of base64: over the 1 MiB card budget.
    photo = "/9j/" + "A" * (1_100_000)
    blob = (
        "BEGIN:VCARD\nVERSION:3.0\nFN:Pictured\n"
        + "PHOTO;ENCODING=b;TYPE=JPEG:" + _folded(photo) + "\n"
        + "TEL;TYPE=CELL:+15551234567\nEMAIL:p@example.com\nEND:VCARD\n"
        # vCard 2.1: unindented base64 continuation lines end at a blank line.
        + "BEGIN:VCARD\nVERSION:2.1\nFN:Old Style\n"
        + "PHOTO;ENCODING=BASE64;TYPE=JPEG:/9j/AAAA\n"
        + "\n".join(["A" * 76] * 20_000) + "\n\n"
        + "TEL;CELL:+15557654321\nEND:VCARD\n"
    )

    assert _parse_vcard_records(blob) == [
        ("Pictured", ["15551234567"], ["p@example.com"]),
        ("Old Style", ["15557654321"], []),
    ]


def test_photo_skipping_keeps_other_fields_and_the_card_budget() -> None:
    blob = (
        "BEGIN:VCARD\nFN:A\nNOTE:x\n PHOTO;folded note text\n"
        "item1.PHOTO;VALUE=uri:https://example.invalid/a.jpg\nTEL:+15550000001\nEND:VCARD\n"
        "BEGIN:VCARD\nFN:" + "y" * 300 + "\nPHOTO;ENCODING=b:QUJD\nEND:VCARD\n"
    )
    bodies = list(iter_vcard_bodies(blob, maximum=10, max_card_chars=200))
    # A folded line that merely starts with "PHOTO" is not a property; a
    # grouped PHOTO is skipped; a non-photo overflow still discards its card.
    assert bodies == ["FN:A\nNOTE:x\n PHOTO;folded note text\nTEL:+15550000001"]


def test_large_logo_sound_and_key_values_are_skipped_like_photos() -> None:
    blob = "".join(
        f"BEGIN:VCARD\nVERSION:3.0\nFN:{name}\n"
        + f"{prop};ENCODING=b;TYPE={kind}:" + _folded("A" * 1_100_000) + "\n"
        + f"TEL:+1555000000{index}\nEND:VCARD\n"
        for index, (name, prop, kind) in enumerate([
            ("Logo Co", "LOGO", "PNG"),
            ("Sound Person", "SOUND", "WAVE"),
            ("Key Holder", "item2.KEY", "PGP"),
        ])
    )

    assert _parse_vcard_records(blob) == [
        ("Logo Co", ["15550000000"], []),
        ("Sound Person", ["15550000001"], []),
        ("Key Holder", ["15550000002"], []),
    ]


def test_quoted_printable_photo_continues_through_soft_line_breaks() -> None:
    # vCard 2.1 QUOTED-PRINTABLE: a line ending in "=" continues unindented,
    # and those lines may contain ":" (as "=3A" does not have to be used).
    body = "=\n".join(["=FF=D8=FF=E0:" + "=00" * 25] * 20_000)
    blob = (
        "BEGIN:VCARD\nVERSION:2.1\nFN:Printable\n"
        + "PHOTO;ENCODING=QUOTED-PRINTABLE;TYPE=JPEG:" + body + "\n"
        + "TEL;CELL:+15551112222\nNOTE:after=\nEND:VCARD\n"
    )

    assert _parse_vcard_records(blob) == [("Printable", ["15551112222"], [])]
    [card] = list(iter_vcard_bodies(blob, maximum=1))
    # The last photo line has no soft break, so TEL starts a new property.
    assert card == "VERSION:2.1\nFN:Printable\nTEL;CELL:+15551112222\nNOTE:after="


def test_skipped_photo_lines_with_crlf_line_endings() -> None:
    photo = "/9j/" + "A" * 1_100_000
    blob = (
        "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Windows Style\r\n"
        + "PHOTO;ENCODING=b:" + _folded(photo).replace("\n", "\r\n") + "\r\n"
        + "EMAIL:w@example.com\r\nEND:VCARD\r\n"
    )

    assert _parse_vcard_records(blob) == [("Windows Style", [], ["w@example.com"])]


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
    resolver._repository = SimpleNamespace(load=lambda **_kwargs: [
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
