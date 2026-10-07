"""Opt-in contact photos: vCard PHOTO parsing, limits, storage, and inertness."""
from __future__ import annotations

import base64
import os
import sqlite3
import stat
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from blueferry import config, contact_photos, contacts, limits
from blueferry.contact_photos import (
    PhotoFiles,
    decode_vcard_photo,
    image_dimensions,
    valid_photo,
)
from blueferry.contact_repository import ContactRepository
from blueferry.contacts import ContactsResolver, _parse_vcard_entries, _parse_vcard_records
from blueferry.settings_store import SettingsStore
from blueferry.storage_security import StorageSecurity
from blueferry.vcard import iter_vcard_bodies, iter_vcard_cards

from .photo_fixtures import jpeg, png, png_header

PROPERTY_SETTINGS = settings(max_examples=150, derandomize=True, deadline=None)


JPEG = jpeg()
PNG = png()


def _fold(text: str, width: int = 75, prefix: str = " ") -> str:
    """RFC 2425/6350 folding: continuation lines start with one space."""
    lines = [text[:width]]
    rest = text[width:]
    while rest:
        lines.append(prefix + rest[:width - 1])
        rest = rest[width - 1:]
    return "\r\n".join(lines)


def _card(*lines: str) -> str:
    return "\r\n".join(("BEGIN:VCARD", "VERSION:3.0", *lines, "END:VCARD")) + "\r\n"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


# ---- decoding ----------------------------------------------------------------

@pytest.mark.parametrize("prop", [
    "PHOTO;ENCODING=b;TYPE=JPEG:{}",                  # vCard 3.0 (iOS)
    "PHOTO;TYPE=JPEG;ENCODING=B:{}",
    "PHOTO;ENCODING=BASE64;TYPE=JPEG:{}",             # vCard 2.1
    "PHOTO;JPEG;ENCODING=BASE64:{}",
    "PHOTO;BASE64:{}",
    "item1.PHOTO;ENCODING=b:{}",                      # grouped property
    "PHOTO:data:image/jpeg;base64,{}",                # vCard 4.0 data URI
])
def test_inline_photo_variants_decode(prop: str) -> None:
    assert decode_vcard_photo(prop.format(_b64(JPEG))) == JPEG


def test_png_and_unpadded_base64_decode() -> None:
    encoded = _b64(PNG).rstrip("=")
    assert decode_vcard_photo(f"PHOTO;ENCODING=b;TYPE=PNG:{encoded}") == PNG


@pytest.mark.parametrize("prop", [
    None,
    "",
    "PHOTO",                                                   # no value
    "PHOTO;VALUE=URI:https://example.invalid/me.jpg",          # never fetched
    "PHOTO;VALUE=uri:file:///etc/passwd",
    "PHOTO:data:image/jpeg,notbase64",
    "PHOTO;ENCODING=QUOTED-PRINTABLE:abc",
    "PHOTO;ENCODING=b:!!!not base64!!!",
    "PHOTO;ENCODING=b:" + _b64(b"GIF89a" + b"\x00" * 32),      # not JPEG/PNG
    "PHOTO;ENCODING=b:" + _b64(b"<svg xmlns='x'/>"),
    "PHOTO;ENCODING=b:" + _b64(JPEG)[:-5] + "*" + _b64(JPEG)[-4:],
])
def test_uris_foreign_formats_and_malformed_base64_are_rejected(prop) -> None:
    assert decode_vcard_photo(prop) is None


def test_decoded_and_encoded_photo_sizes_are_bounded(monkeypatch) -> None:
    frame = jpeg(4, 4, padding=0)
    large = jpeg(4, 4, padding=limits.MAX_CONTACT_PHOTO_BYTES - len(frame) + 1)
    assert len(large) == limits.MAX_CONTACT_PHOTO_BYTES + 1
    fits = jpeg(4, 4, padding=limits.MAX_CONTACT_PHOTO_BYTES - len(frame))
    assert decode_vcard_photo("PHOTO;ENCODING=b:" + _b64(large)) is None
    assert decode_vcard_photo("PHOTO;ENCODING=b:" + _b64(fits)) == fits
    # The encoded check happens before any base64 work.
    monkeypatch.setattr(contact_photos, "MAX_CONTACT_PHOTO_CHARS", 32)
    assert decode_vcard_photo("PHOTO;ENCODING=b:" + _b64(JPEG)) is None


@pytest.mark.parametrize("data,expected", [
    (png(7, 5), (7, 5)),
    (png_header(2048, 1), (2048, 1)),
    (jpeg(640, 480), (640, 480)),
    (jpeg(12, 34, sof=0xC2), (12, 34)),                       # progressive
    (jpeg(9, 9)[:2] + b"\xff\xff" + jpeg(9, 9)[2:], (9, 9)),  # fill byte
    (b"\xff\xd8\xff\xd0" + jpeg(5, 6)[2:], (5, 6)),           # standalone RST
    (jpeg(9, 9)[:28], None),                                   # truncated frame
    (b"\xff\xd8\xff\xda\x00\x02", None),                     # scan before frame
    (b"\xff\xd8\xff\xe0\x00\x01", None),                     # bad length
    (b"\xff\xd8\x00\x00", None),                               # not a marker
    (b"\x89PNG\r\n\x1a\n" + b"\x00" * 16, None),                # no IHDR
    (b"GIF89a", None),
])
def test_image_dimensions_read_only_the_header(data, expected) -> None:
    assert image_dimensions(data) == expected


@pytest.mark.parametrize("data", [
    png_header(2049, 1), png_header(1, 30_000), png_header(0, 5),
    jpeg(65_535, 65_535), jpeg(2049, 16), jpeg(0, 16),
    b"\xff\xd8\xff\xe0" + b"\x00" * 64,                       # JPEG without a frame
    b"\x89PNG\r\n\x1a\n" + b"\x00" * 64,
])
def test_declared_canvas_bombs_are_rejected_everywhere(data) -> None:
    assert valid_photo(data) is None
    assert decode_vcard_photo("PHOTO;ENCODING=b:" + _b64(data)) is None


def test_quoted_parameters_may_contain_colons_and_semicolons() -> None:
    prop = 'PHOTO;X-LABEL="a:b;c";ENCODING=b;TYPE=JPEG:' + _b64(JPEG)
    assert decode_vcard_photo(prop) == JPEG
    # A quoted "ENCODING=b" is a label value, not the encoding parameter.
    assert decode_vcard_photo('PHOTO;X-L="x;ENCODING=b":' + _b64(JPEG)) is None


def test_valid_photo_checks_type_size_and_signature() -> None:
    assert valid_photo(JPEG) == JPEG
    assert valid_photo(bytearray(PNG)) == PNG
    for value in (None, "", b"", "\xff\xd8\xff", b"\xff\xd8", b"BM" + b"\x00" * 10):
        assert valid_photo(value) is None


# ---- card splitting and folding ------------------------------------------------

def test_folded_vcard30_photo_is_unfolded_and_kept_out_of_the_body() -> None:
    photo = _fold("PHOTO;ENCODING=b;TYPE=JPEG:" + _b64(JPEG))
    blob = _card("FN:Alice", photo, "TEL;TYPE=CELL:+1 555 111 2222")
    [(body, prop)] = list(iter_vcard_cards(blob, maximum=10, max_photo_chars=10**6))
    assert "PHOTO" not in body and "TEL;TYPE=CELL" in body
    assert decode_vcard_photo(prop) == JPEG
    [(record, data)] = _parse_vcard_entries(blob)
    assert record == ("Alice", ["15551112222"], []) and data == JPEG


def test_tab_folding_is_accepted() -> None:
    blob = _card("FN:Tab", _fold("PHOTO;ENCODING=b:" + _b64(PNG), prefix="\t"))
    assert _parse_vcard_entries(blob)[0][1] == PNG


def test_vcard21_unindented_base64_ends_at_the_blank_line() -> None:
    encoded = _b64(JPEG)
    chunks = [encoded[i:i + 76] for i in range(0, len(encoded), 76)]
    blob = "\r\n".join([
        "BEGIN:VCARD", "VERSION:2.1", "N:Doe;Jane", "FN:Jane Doe",
        "PHOTO;ENCODING=BASE64;TYPE=JPEG:" + chunks[0], *chunks[1:], "",
        "TEL;CELL:+15553334444", "END:VCARD",
    ])
    [(record, data)] = _parse_vcard_entries(blob)
    assert data == JPEG
    assert record == ("Jane Doe", ["15553334444"], [])


def test_only_the_first_photo_is_kept_and_a_second_is_not_merged_into_it() -> None:
    blob = _card(
        "FN:Two",
        _fold("PHOTO;ENCODING=b:" + _b64(JPEG)),
        _fold("PHOTO;ENCODING=b:" + _b64(PNG)),
        "EMAIL:two@example.com",
    )
    [(record, data)] = _parse_vcard_entries(blob)
    assert data == JPEG
    assert record == ("Two", [], ["two@example.com"])


def test_a_folded_line_that_looks_like_photo_is_not_a_photo() -> None:
    blob = _card("FN:Note", "NOTE:see", " PHOTO;ENCODING=b:" + _b64(JPEG))
    [(body, prop)] = list(iter_vcard_cards(blob, maximum=5, max_photo_chars=10**6))
    # The fold joins the line to NOTE, so it is note text, not a property.
    assert prop is None and "NOTE:seePHOTO;ENCODING=b:" in body


def test_oversized_photo_is_dropped_but_the_contact_survives() -> None:
    huge = "PHOTO;ENCODING=b:" + "A" * 5000
    blob = _card("FN:Big", _fold(huge), "TEL:+15550001111") + _card("FN:Next", "TEL:+15550002222")
    cards = list(iter_vcard_cards(blob, maximum=10, max_photo_chars=1000))
    assert [photo for _body, photo in cards] == [None, None]
    assert [record for record, _photo in _parse_vcard_entries(blob)] == [
        ("Big", ["15550001111"], []), ("Next", ["15550002222"], []),
    ]


def test_photo_does_not_count_against_the_card_budget() -> None:
    photo = _fold("PHOTO;ENCODING=b:" + _b64(JPEG))
    blob = _card("FN:Alice", photo, "TEL:+15551112222")
    # The photo is larger than the card budget; neither iterator counts it,
    # and both yield the same body.
    [blind] = list(iter_vcard_bodies(blob, maximum=5, max_card_chars=200))
    [(body, prop)] = list(iter_vcard_cards(
        blob, maximum=5, max_card_chars=200, max_photo_chars=10**6,
    ))
    assert body == blind and "FN:Alice" in body
    assert decode_vcard_photo(prop) == JPEG


def test_records_match_the_photo_blind_parser_for_ordinary_cards() -> None:
    blob = (
        _card("FN:Alice", _fold("PHOTO;ENCODING=b:" + _b64(JPEG)), "TEL:+15551112222")
        + _card("FN:Bob", "EMAIL:bob@example.com")
        + _card("NOTE:no identity")
    )
    assert [record for record, _photo in _parse_vcard_entries(blob)] == _parse_vcard_records(blob)
    assert [photo for _record, photo in _parse_vcard_entries(blob)] == [JPEG, None]


def test_total_photo_budget_drops_later_photos(monkeypatch) -> None:
    monkeypatch.setattr(contacts, "MAX_CONTACT_PHOTOS_TOTAL_BYTES", len(JPEG) + len(PNG) - 1)
    blob = "".join(
        _card(f"FN:P{index}", _fold("PHOTO;ENCODING=b:" + _b64(data)), f"TEL:+1555000{index:04d}")
        for index, data in enumerate((JPEG, PNG, PNG))
    )
    photos = [photo for _record, photo in _parse_vcard_entries(blob)]
    assert photos[0] == JPEG and photos[1] is None
    assert len(photos) == 3


@PROPERTY_SETTINGS
@given(st.text(max_size=2_048))
def test_arbitrary_text_cannot_crash_photo_parsing(blob: str) -> None:
    entries = _parse_vcard_entries(blob)
    assert all(len(record) == 3 for record, _photo in entries)
    assert all(photo is None or valid_photo(photo) == photo for _record, photo in entries)
    assert decode_vcard_photo(blob) is None or valid_photo(decode_vcard_photo(blob))


_CARD_LINES = st.lists(
    st.sampled_from([
        "BEGIN:VCARD", "END:VCARD", "VERSION:2.1", "VERSION:3.0", "FN:A",
        "TEL:+15551112222", "PHOTO;ENCODING=b:/9j/", "item1.PHOTO;ENCODING=BASE64:AAAA",
        "LOGO;ENCODING=b:AAAA", "PHOTO;ENCODING=QUOTED-PRINTABLE:=FF=", " AAAA", "\tBBBB",
        "QUJD", "", "=00", 'NOTE;X="a:b":c',
    ]),
    max_size=40,
)


@PROPERTY_SETTINGS
@given(_CARD_LINES, st.sampled_from(["\r\n", "\n"]), st.integers(1, 40))
def test_photo_aware_bodies_equal_the_photo_blind_bodies(lines, newline, limit) -> None:
    import io

    from blueferry.vcard import iter_bounded_lines

    blob = newline.join(lines)
    blind = list(iter_vcard_bodies(blob, maximum=50, max_card_chars=120))
    cards = list(iter_vcard_cards(blob, maximum=50, max_card_chars=120, max_photo_chars=30))
    assert [body for body, _photo in cards] == blind
    assert all(photo is None or len(photo) <= 30 for _body, photo in cards)
    streamed = list(iter_vcard_cards(
        iter_bounded_lines(io.StringIO(blob, newline=None), limit=limit),
        maximum=50, max_card_chars=120, max_photo_chars=30,
    ))
    assert [body for body, _photo in streamed] == list(iter_vcard_bodies(
        iter_bounded_lines(io.StringIO(blob, newline=None), limit=limit),
        maximum=50, max_card_chars=120,
    ))


def test_an_oversized_first_photo_does_not_promote_a_later_one() -> None:
    blob = _card(
        "FN:Two",
        _fold("PHOTO;ENCODING=b:" + "A" * 5000),
        _fold("PHOTO;ENCODING=b:" + _b64(PNG)),
        "TEL:+15550003333",
    )
    [(body, prop)] = list(iter_vcard_cards(blob, maximum=5, max_photo_chars=1000))
    assert prop is None and body == "VERSION:3.0\nFN:Two\nTEL:+15550003333"


@PROPERTY_SETTINGS
@given(st.binary(max_size=512), st.sampled_from(["", " ", "\r\n ", "\n\t"]))
def test_arbitrary_base64_payloads_only_yield_accepted_images(data: bytes, fold: str) -> None:
    encoded = _b64(data)
    folded = fold.join(encoded[i:i + 7] for i in range(0, len(encoded), 7))
    decoded = decode_vcard_photo("PHOTO;ENCODING=b:" + folded)
    assert decoded == (data if valid_photo(data) else None)


# ---- storage -----------------------------------------------------------------

class _Wallet:
    def get_or_create(self, *, allow_prompt: bool, cancellable=None) -> bytes:
        return b"K" * 32

    def delete(self, *, allow_prompt: bool, cancellable=None) -> bool:
        return True


@pytest.fixture(autouse=True)
def photos_enabled(monkeypatch):
    monkeypatch.setattr(config, "CONTACT_PHOTOS", True)


@pytest.fixture
def storage(isolated_state):
    selected = StorageSecurity(
        settings=SettingsStore(config.SETTINGS_JSON), key_provider=_Wallet(),
    )
    assert selected.status.can_write
    yield selected
    selected.close()


def _photo_table_exists() -> bool:
    if not config.CONTACTS_DB.exists():
        return False
    with closing(sqlite3.connect(config.CONTACTS_DB)) as connection:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'contact_photos'"
        ).fetchone() is not None


def _photo_rows() -> list[bytes]:
    if not _photo_table_exists():
        return []
    with closing(sqlite3.connect(config.CONTACTS_DB)) as connection:
        return [row[0] for row in connection.execute("SELECT payload FROM contact_photos")]


def test_encrypted_photos_round_trip_owner_only_and_sealed(storage) -> None:
    ContactRepository(storage).replace(
        [("Alice", ["15551112222"], ["alice@example.com"]), ("Bob", ["15553334444"], [])],
        photos=[JPEG, None],
    )
    assert stat.S_IMODE(config.CONTACTS_DB.stat().st_mode) == 0o600
    assert stat.S_IMODE(config.STATE_DIR.stat().st_mode) == 0o700
    [payload] = _photo_rows()
    # Raw sealed bytes in a BLOB: prefix + nonce + ciphertext + tag, no base64.
    assert isinstance(payload, bytes) and payload.startswith(b"blueferry:aesgcm:v1:")
    assert len(payload) == len(b"blueferry:aesgcm:v1:") + 12 + len(JPEG) + 16
    raw = config.CONTACTS_DB.read_bytes()
    assert JPEG[:64] not in raw and _b64(JPEG)[:64].encode() not in raw

    resolver = ContactsResolver(storage=storage)
    assert resolver.photo("+1 (555) 111-2222") == JPEG
    assert resolver.photo("ALICE@example.com") == JPEG
    assert resolver.photo("5551112222") == JPEG  # NANP variant of the same record
    assert resolver.photo("+15553334444") is None
    assert resolver.photo("+19999999999") is None
    assert resolver.photo_count() == 1


def test_disabled_resolver_ignores_stored_photos(storage, monkeypatch) -> None:
    ContactRepository(storage).replace([("Alice", ["15551112222"], [])], photos=[JPEG])
    monkeypatch.setattr(config, "CONTACT_PHOTOS", False)
    resolver = ContactsResolver(storage=storage)
    assert resolver.photo_ref("+15551112222") is None
    assert resolver.photo("+15551112222") is None and resolver.photo_count() == 0
    assert resolver.records() == [("Alice", ["15551112222"], [])]


def test_plaintext_policy_keeps_photos_under_the_same_lifecycle(isolated_state) -> None:
    storage = StorageSecurity(
        settings=SettingsStore(config.SETTINGS_JSON), key_provider=_Wallet(),
    )
    try:
        storage.set_policy("plaintext")
        ContactRepository(storage).replace([("Alice", ["15551112222"], [])], photos=[PNG])
        assert ContactsResolver(storage=storage).photo("+15551112222") == PNG
    finally:
        storage.close()


def test_ambiguous_address_never_shows_either_photo(storage) -> None:
    ContactRepository(storage).replace(
        [("Alice", ["15551112222"], []), ("Alicia", ["15551112222"], ["a@example.com"])],
        photos=[JPEG, PNG],
    )
    resolver = ContactsResolver(storage=storage)
    assert resolver.photo("+15551112222") is None
    assert resolver.photo("a@example.com") == PNG


def test_invalid_photo_bytes_are_never_stored(storage) -> None:
    ContactRepository(storage).replace(
        [("Alice", ["15551112222"], [])], photos=[b"GIF89a" + b"\x00" * 20],
    )
    assert _photo_rows() == []


def test_replace_clear_and_clear_photos_erase_photos(storage) -> None:
    repository = ContactRepository(storage)
    repository.replace([("Alice", ["15551112222"], [])], photos=[JPEG])
    repository.replace([("Alice", ["15551112222"], [])])
    assert _photo_rows() == []

    repository.replace([("Alice", ["15551112222"], [])], photos=[JPEG])
    assert repository.clear_photos() is True
    assert _photo_rows() == []
    assert repository.clear_photos() is False
    # A dangling reference in the contact payload is dropped when loading,
    # so it is neither counted nor offered.
    resolver = ContactsResolver(storage=storage)
    assert resolver.photo_ref("+15551112222") is None
    assert resolver.photo("+15551112222") is None and resolver.photo_count() == 0

    repository.replace([("Alice", ["15551112222"], [])], photos=[JPEG])
    repository.clear()
    assert _photo_rows() == []


def test_a_version_without_photos_erases_them_with_its_contact_clear(isolated_state) -> None:
    # Plaintext policy: photo bytes are not sealed, so a leftover row would
    # be readable. Run the statements of a contact cache clear from a
    # version that does not know about photos.
    storage = StorageSecurity(
        settings=SettingsStore(config.SETTINGS_JSON), key_provider=_Wallet(),
    )
    try:
        storage.set_policy("plaintext")
        ContactRepository(storage).replace(
            [("Alice", ["15551112222"], []), ("Bob", ["15553334444"], [])],
            photos=[JPEG, PNG],
        )
    finally:
        storage.close()
    assert len(_photo_rows()) == 2
    with closing(sqlite3.connect(config.CONTACTS_DB)) as connection:
        connection.execute("PRAGMA secure_delete = ON")
        with connection:
            for table in ("phones", "emails", "contacts", "secure_contacts"):
                connection.execute(f"DELETE FROM {table}")
        connection.execute("VACUUM")
    assert _photo_rows() == []
    assert PNG not in config.CONTACTS_DB.read_bytes()


def test_our_own_replacement_keeps_the_new_photos(storage) -> None:
    repository = ContactRepository(storage)
    repository.replace([("Alice", ["15551112222"], [])], photos=[JPEG])
    repository.replace([("Bob", ["15553334444"], [])], photos=[PNG])
    assert len(_photo_rows()) == 1
    [(_record, ref)] = repository.load_entries()
    assert ref is not None and repository.load_photo(ref) == PNG


def test_clear_photos_does_not_create_a_missing_database(isolated_state) -> None:
    assert not config.CONTACTS_DB.exists()
    assert ContactRepository().clear_photos() is False
    assert not config.CONTACTS_DB.exists()


def test_tampered_photo_fails_storage_closed(storage) -> None:
    ContactRepository(storage).replace([("Alice", ["15551112222"], [])], photos=[JPEG])
    resolver = ContactsResolver(storage=storage)
    [payload] = _photo_rows()
    forged = payload[:-8] + bytes([payload[-8] ^ 1]) + payload[-7:]
    with closing(sqlite3.connect(config.CONTACTS_DB)) as connection, connection:
        connection.execute("UPDATE contact_photos SET payload = ?", (forged,))
    assert resolver.photo("+15551112222") is None
    assert not storage.status.can_read


def test_locked_storage_serves_no_photo(storage) -> None:
    ContactRepository(storage).replace([("Alice", ["15551112222"], [])], photos=[JPEG])
    resolver = ContactsResolver(storage=storage)
    ref = resolver.photo_ref("+15551112222")
    storage.fail_closed("locked for test")
    assert resolver.load_photo(ref) is None


def test_refresh_bumps_the_content_free_photo_revision(storage) -> None:
    resolver = ContactsResolver(storage=storage)
    before = resolver.photo_revision
    ContactRepository(storage).replace([("Alice", ["15551112222"], [])], photos=[JPEG])
    resolver.refresh()
    assert resolver.photo_revision == before + 1
    assert resolver.snapshot().photo("+15551112222") == JPEG


# ---- PBAP pull: enabled vs. disabled -------------------------------------------

def _fake_pull(monkeypatch, tmp_path, blob: str):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    requested = []

    class _Pbap:
        def Select(self, *_args, **_kwargs):
            pass

        def PullAll(self, path, filters, **_kwargs):
            requested.append(dict(filters))
            Path(path).write_text(blob)
            return "/transfer/phonebook", {"Status": "complete", "Size": len(blob)}

    monkeypatch.setattr(contacts, "obex", lambda *_args: _Pbap())
    monkeypatch.setattr(contacts, "wait_for_transfer", lambda *_args, **_kwargs: "complete")
    return requested


@pytest.mark.parametrize("enabled", [False, True])
def test_pull_request_is_identical_and_disabled_path_stores_no_photo(
    storage, monkeypatch, tmp_path, enabled,
) -> None:
    blob = _card("FN:Alice", _fold("PHOTO;ENCODING=b;TYPE=JPEG:" + _b64(JPEG)), "TEL:+15551112222")
    requested = _fake_pull(monkeypatch, tmp_path, blob)
    monkeypatch.setattr(config, "CONTACT_PHOTOS", enabled)

    assert contacts.pull_phonebook(SimpleNamespace(pbap_path="/pbap"), storage=storage) == 1

    # No Fields filter either way: the photo arrives in the one bulk pull.
    assert set(requested[0]) == {"MaxCount", "Format"}
    assert bool(_photo_rows()) is enabled
    # The off path leaves the upstream schema untouched.
    assert _photo_table_exists() is enabled
    photo = ContactsResolver(storage=storage).photo("+15551112222")
    assert photo == (JPEG if enabled else None)


# ---- volatile notification copies ----------------------------------------------

def test_photo_files_are_private_reused_bounded_and_cleared(isolated_state, monkeypatch) -> None:
    monkeypatch.setattr(contact_photos, "MAX_CONTACT_PHOTO_FILES", 2)
    refs = {"+15551112222": 1, "b": 2, "c": 3, "gif": 4}
    data = {1: JPEG, 2: PNG, 3: JPEG, 4: b"GIF89a"}
    loads = []

    def load(ref):
        loads.append(ref)
        return data[ref]

    files = PhotoFiles(photo_ref=refs.get, load_photo=load)
    first = Path(files.path_for("+15551112222"))
    assert first.read_bytes() == JPEG and first.suffix == ".jpg"
    assert stat.S_IMODE(first.stat().st_mode) == 0o600
    assert stat.S_IMODE(first.parent.stat().st_mode) == 0o700
    assert first.parent == Path(os.environ["XDG_RUNTIME_DIR"]) / "blueferry"
    assert "5551112222" not in first.name
    assert files.path_for("+15551112222") == str(first) and loads == [1]
    assert Path(files.path_for("b")).suffix == ".png"
    assert files.path_for("unknown") is None
    assert files.path_for("gif") is None

    files.path_for("c")
    assert len(files) == 2 and not first.exists()
    remaining = [Path(files.path_for(key)) for key in ("b", "c")]
    files.clear()
    assert len(files) == 0 and not any(path.exists() for path in remaining)


def test_refresh_retires_files_so_shown_popups_keep_their_icon(isolated_state, monkeypatch) -> None:
    monkeypatch.setattr(contact_photos, "MAX_CONTACT_PHOTO_FILES", 3)
    refs = {"a": 1, "b": 2}
    files = PhotoFiles(photo_ref=refs.get, load_photo={1: JPEG, 2: PNG, 3: JPEG, 4: PNG}.get)
    shown = Path(files.path_for("a"))
    files.retire()
    # The shown popup's file is still there for a lazy notification server...
    assert shown.read_bytes() == JPEG and len(files) == 1
    # ...but a refreshed cache never hands it out again.
    refs.update(a=3, b=4)
    fresh = Path(files.path_for("a"))
    assert fresh != shown and fresh.read_bytes() == JPEG
    # The bound evicts retired files first, then current ones.
    other = Path(files.path_for("b"))
    assert len(files) == 3 and shown.exists()
    refs["c"] = 1
    files.path_for("c")
    assert not shown.exists() and fresh.exists() and other.exists()
    files.retire()
    files.clear()
    assert len(files) == 0 and not fresh.exists() and not other.exists()


def test_photo_file_failures_degrade_to_no_icon(isolated_state) -> None:
    def broken(_ref):
        raise RuntimeError("storage locked")

    assert PhotoFiles(photo_ref=lambda _address: 1, load_photo=broken).path_for("a") is None


def test_photo_file_negative_cache_until_clear(isolated_state) -> None:
    loads = []

    def load(ref):
        loads.append(ref)
        return None  # dangling reference

    files = PhotoFiles(photo_ref=lambda _address: 7, load_photo=load)
    assert files.path_for("a") is None and files.path_for("a") is None
    assert loads == [7]
    files.clear()
    assert files.path_for("a") is None and loads == [7, 7]


def test_busy_store_is_not_negatively_cached(isolated_state) -> None:
    from blueferry.contact_repository import PhotoStoreBusy

    attempts = []

    def load(ref):
        attempts.append(ref)
        raise PhotoStoreBusy("locked")

    files = PhotoFiles(photo_ref=lambda _address: 3, load_photo=load)
    assert files.path_for("a") is None and files.path_for("a") is None
    assert attempts == [3, 3]


def test_orphaned_avatar_files_from_a_crash_are_swept(isolated_state) -> None:
    runtime = Path(os.environ["XDG_RUNTIME_DIR"]) / "blueferry"
    runtime.mkdir(mode=0o700)
    orphan = runtime / "avatar-old.jpg"
    orphan.write_bytes(JPEG)
    unrelated = runtime / "phonebook-x"
    unrelated.write_bytes(b"keep")
    target = runtime / "elsewhere"
    target.write_bytes(b"keep")
    (runtime / "avatar-link.jpg").symlink_to(target)

    PhotoFiles(photo_ref=lambda _address: None, load_photo=lambda _ref: None)

    assert not orphan.exists()
    assert unrelated.exists() and target.read_bytes() == b"keep"
    assert (runtime / "avatar-link.jpg").is_symlink()  # only regular files are removed


def test_photo_read_reports_busy_instead_of_missing_while_a_sync_writes(storage) -> None:
    from blueferry.contact_repository import PhotoStoreBusy

    ContactRepository(storage).replace([("Alice", ["15551112222"], [])], photos=[JPEG])
    resolver = ContactsResolver(storage=storage)
    writer = sqlite3.connect(config.CONTACTS_DB)
    try:
        writer.execute("BEGIN EXCLUSIVE")
        with pytest.raises(PhotoStoreBusy):
            resolver.photo("+15551112222")
    finally:
        writer.rollback()
        writer.close()
    assert resolver.photo("+15551112222") == JPEG


def test_photo_read_opens_the_database_read_only(storage) -> None:
    ContactRepository(storage).replace([("Alice", ["15551112222"], [])], photos=[JPEG])
    resolver = ContactsResolver(storage=storage)
    config.CONTACTS_DB.chmod(0o400)
    try:
        assert resolver.photo("+15551112222") == JPEG
    finally:
        config.CONTACTS_DB.chmod(0o600)
    config.CONTACTS_DB.unlink()
    assert resolver.photo("+15551112222") is None
    assert not config.CONTACTS_DB.exists()


def test_unindented_base64_only_lines_after_a_photo_are_consumed() -> None:
    # vCard 2.1 data lines are unindented; a stray base64-looking line (no
    # colon, so it cannot be a property) is consumed with the photo.
    blob = "\r\n".join([
        "BEGIN:VCARD", "VERSION:2.1", "FN:Stray",
        "PHOTO;ENCODING=BASE64;TYPE=JPEG:" + _b64(JPEG), "ORPHANLINE",
        "TEL:+15550009999", "END:VCARD",
    ])
    [(body, prop)] = list(iter_vcard_cards(blob, maximum=5, max_photo_chars=10**6))
    assert "ORPHANLINE" not in body and "TEL:+15550009999" in body
    assert decode_vcard_photo(prop) is None  # the stray text corrupted the data


def test_binary_sealing_round_trips_and_is_bound_to_policy_and_purpose(storage) -> None:
    from blueferry.storage_security import CorruptStorageError

    sealed = storage.encrypt_bytes(JPEG, purpose="contact-photo-v1")
    assert sealed != JPEG and storage.decrypt_bytes(sealed, purpose="contact-photo-v1") == JPEG
    with pytest.raises(CorruptStorageError):
        storage.decrypt_bytes(sealed, purpose="contact-record-v1")
    with pytest.raises(CorruptStorageError):
        storage.decrypt_bytes(JPEG, purpose="contact-photo-v1")
    storage.set_policy("plaintext")
    assert storage.encrypt_bytes(JPEG, purpose="contact-photo-v1") == JPEG
    with pytest.raises(CorruptStorageError):
        storage.decrypt_bytes(sealed, purpose="contact-photo-v1")


def test_logo_sound_and_key_are_consumed_but_never_taken_as_the_photo() -> None:
    blob = _card(
        "FN:Media",
        _fold("LOGO;ENCODING=b;TYPE=PNG:" + _b64(PNG)),
        _fold("PHOTO;ENCODING=b;TYPE=JPEG:" + _b64(JPEG)),
        _fold("item3.KEY;ENCODING=b:" + "A" * 3000),
        _fold("SOUND;ENCODING=b:" + "B" * 3000),
        "TEL:+15550004444",
    )
    [(body, prop)] = list(iter_vcard_cards(blob, maximum=5, max_photo_chars=10**6))
    assert body == "VERSION:3.0\nFN:Media\nTEL:+15550004444"
    assert decode_vcard_photo(prop) == JPEG

    only_logo = _card("FN:Logo only", _fold("LOGO;ENCODING=b:" + _b64(PNG)))
    assert [photo for _record, photo in _parse_vcard_entries(only_logo)] == [None]


def test_quoted_printable_photo_is_consumed_and_rejected() -> None:
    blob = "\r\n".join([
        "BEGIN:VCARD", "VERSION:2.1", "FN:Printable",
        "PHOTO;ENCODING=QUOTED-PRINTABLE;TYPE=JPEG:=FF=D8=FF:=",
        "=00=00=",
        "=00",
        "TEL;CELL:+15551112222", "END:VCARD",
    ])
    [(body, prop)] = list(iter_vcard_cards(blob, maximum=5, max_photo_chars=10**6))
    assert body == "VERSION:2.1\nFN:Printable\nTEL;CELL:+15551112222"
    assert prop is not None and decode_vcard_photo(prop) is None


_HOT_JOURNAL_WRITER = """
import os, signal, sqlite3, sys
connection = sqlite3.connect(sys.argv[1], isolation_level=None)
connection.execute("PRAGMA cache_size = 1")
connection.execute("BEGIN IMMEDIATE")
for _ in range(64):
    connection.execute("INSERT INTO secure_contacts(payload) VALUES (?)", ("x" * 65536,))
connection.execute("UPDATE contact_photos SET payload = zeroblob(length(payload))")
os.kill(os.getpid(), signal.SIGKILL)
"""


def test_hot_journal_after_a_crash_is_busy_not_missing(storage) -> None:
    import subprocess
    import sys

    from blueferry.contact_repository import PhotoStoreBusy

    ContactRepository(storage).replace([("Alice", ["15551112222"], [])], photos=[JPEG])
    resolver = ContactsResolver(storage=storage)
    # A writer that dies mid-transaction leaves a hot rollback journal.
    subprocess.run(
        [sys.executable, "-c", _HOT_JOURNAL_WRITER, str(config.CONTACTS_DB)],
        check=False,
    )
    assert Path(f"{config.CONTACTS_DB}-journal").exists()

    with pytest.raises(PhotoStoreBusy):
        resolver.photo("+15551112222")
    # The daemon's next writable open rolls the journal back.
    ContactRepository(storage).load_entries()
    assert resolver.photo("+15551112222") == JPEG


@pytest.mark.parametrize("name,message,busy", [
    ("SQLITE_BUSY", "database is locked", True),
    ("SQLITE_LOCKED_SHAREDCACHE", "database table is locked", True),
    ("SQLITE_READONLY_ROLLBACK", "attempt to write a readonly database", True),
    ("SQLITE_READONLY_RECOVERY", "attempt to write a readonly database", True),
    ("SQLITE_CORRUPT", "database disk image is malformed", False),
    ("SQLITE_ERROR", "no such table: contact_photos", False),
    (None, "database is locked", True),
    (None, "no such table", False),
])
def test_busy_classification_uses_sqlite_error_names(name, message, busy) -> None:
    from blueferry.contact_repository import _is_busy_error

    error = sqlite3.OperationalError(message)
    if name is not None:
        error.sqlite_errorname = name
    assert _is_busy_error(error) is busy


def test_unbalanced_quote_in_parameters_discards_the_photo() -> None:
    assert decode_vcard_photo('PHOTO;X-L="abc:ENCODING=b:' + _b64(JPEG)) is None


def test_photo_decoding_stops_at_the_time_budget() -> None:
    blob = "".join(
        _card(f"FN:P{index}", _fold("PHOTO;ENCODING=b:" + _b64(JPEG)), f"TEL:+1555000{index:04d}")
        for index in range(4)
    )
    # The clock is read around each decode only: the second takes 6 s.
    ticks = iter([0.0, 0.0, 0.0, 6.0])
    stats = contacts.PhotoStats()
    entries = _parse_vcard_entries(blob, clock=lambda: next(ticks), stats=stats)
    assert [photo for _record, photo in entries] == [JPEG, JPEG, None, None]
    assert len(entries) == 4  # contacts are kept even when photos are skipped
    assert stats.dropped == {"time": 2}


def test_card_parsing_time_does_not_count_against_the_decode_budget() -> None:
    blob = "".join(
        _card(f"FN:P{index}", _fold("PHOTO;ENCODING=b:" + _b64(JPEG)), f"TEL:+1555000{index:04d}")
        for index in range(4)
    )
    # 100 s pass between cards, but every decode itself is instantaneous.
    ticks = iter([0.0, 0.0, 100.0, 100.0, 200.0, 200.0, 300.0, 300.0])
    entries = _parse_vcard_entries(blob, clock=lambda: next(ticks))
    assert [photo for _record, photo in entries] == [JPEG] * 4


def test_photo_summary_counts_sizes_and_reasons_without_contact_data() -> None:
    blob = (
        _card("FN:Alice Secret", _fold("PHOTO;ENCODING=b:" + _b64(JPEG)), "TEL:+15551112222")
        + _card("FN:Uri", "PHOTO;VALUE=URI:https://example.com/a.jpg", "TEL:+15551113333")
        + _card("FN:Gif", "PHOTO;ENCODING=b:" + _b64(b"GIF89a" + bytes(32)), "TEL:+15551114444")
        + _card("FN:Bomb", "PHOTO;ENCODING=b:" + _b64(png_header(4096, 4096)), "TEL:+15551115555")
        + _card("FN:Huge", _fold("PHOTO;ENCODING=b:" + "A" * (limits.MAX_CONTACT_PHOTO_CHARS + 10)),
                "TEL:+15551116666")
    )
    stats = contacts.PhotoStats()
    entries = _parse_vcard_entries(blob, stats=stats)
    assert [photo for _record, photo in entries] == [JPEG, None, None, None, None]
    assert stats.dropped == {
        "not-inline": 1, "format": 1, "dimensions": 1, "too-large": 1,
    }
    summary = stats.summary()
    assert "<=64KiB:3" in summary and ">4MiB:0" in summary
    assert "dimensions:1" in summary and "too-large:1" in summary
    assert "Alice" not in summary and "1555" not in summary


@pytest.mark.parametrize("prop,reason", [
    ("PHOTO;VALUE=URI:https://example.com/a.jpg", "not-inline"),
    ("PHOTO;ENCODING=b:!!!!", "malformed"),
    ("PHOTO;ENCODING=b:" + _b64(b"GIF89a" + bytes(32)), "format"),
    ("PHOTO;ENCODING=b:" + _b64(png_header(4096, 16)), "dimensions"),
])
def test_inspect_reports_why_a_photo_was_rejected(prop, reason) -> None:
    data, rejected, _size = contact_photos.inspect_vcard_photo(prop)
    assert data is None and rejected == reason


def test_decoded_photo_over_the_cap_is_reported_too_large(monkeypatch) -> None:
    monkeypatch.setattr(contact_photos, "MAX_CONTACT_PHOTO_BYTES", len(JPEG) - 1)
    data, reason, size = contact_photos.inspect_vcard_photo("PHOTO;ENCODING=b:" + _b64(JPEG))
    assert (data, reason, size) == (None, "too-large", len(JPEG))


def test_photos_that_can_never_be_shown_are_not_decoded_or_budgeted(monkeypatch) -> None:
    # Two cards share their only number, so neither photo could be served;
    # their bytes must not use the budget a later contact needs.
    monkeypatch.setattr(contacts, "MAX_CONTACT_PHOTOS_TOTAL_BYTES", len(PNG))
    blob = (
        _card("FN:Twin A", "PHOTO;ENCODING=b:" + _b64(PNG), "TEL:+15550001111")
        + _card("FN:Twin B", "PHOTO;ENCODING=b:" + _b64(PNG), "TEL:+15550001111")
        + _card("FN:Carol", "PHOTO;ENCODING=b:" + _b64(PNG), "TEL:+15550003333")
    )
    servable = contacts._servable_photo_indices(_parse_vcard_records(blob))
    assert servable == {2}
    stats = contacts.PhotoStats()
    entries = _parse_vcard_entries(blob, servable=servable, stats=stats)
    assert [photo for _record, photo in entries] == [None, None, PNG]
    assert stats.dropped == {"shared": 2}


def test_budget_exhaustion_is_logged_with_a_count(monkeypatch, caplog) -> None:
    monkeypatch.setattr(contacts, "MAX_CONTACT_PHOTOS_TOTAL_BYTES", len(JPEG))
    blob = "".join(
        _card(f"FN:P{index}", "PHOTO;ENCODING=b:" + _b64(JPEG), f"TEL:+1555000{index:04d}")
        for index in range(3)
    )
    with caplog.at_level("WARNING", logger="blueferry.contacts"):
        entries = _parse_vcard_entries(blob)
    assert [photo for _record, photo in entries] == [JPEG, None, None]
    assert "2 later photos were not kept" in caplog.text


def test_pull_logs_a_content_free_photo_summary(storage, monkeypatch, tmp_path, caplog) -> None:
    blob = (
        _card("FN:Alice", _fold("PHOTO;ENCODING=b:" + _b64(JPEG)), "TEL:+15551112222")
        + _card("FN:Twin A", "PHOTO;ENCODING=b:" + _b64(PNG), "TEL:+15550001111")
        + _card("FN:Twin B", "PHOTO;ENCODING=b:" + _b64(PNG), "TEL:+15550001111")
    )
    _fake_pull(monkeypatch, tmp_path, blob)
    monkeypatch.setattr(config, "CONTACT_PHOTOS", True)
    with caplog.at_level("INFO", logger="blueferry.contacts"):
        assert contacts.pull_phonebook(SimpleNamespace(pbap_path="/pbap"), storage=storage) == 3
    [line] = [record.getMessage() for record in caplog.records if "with photos" in record.getMessage()]
    assert "parsed 3 contacts (1 with photos)" in line
    assert "<=64KiB:1" in line and "shared:2" in line
    assert "Alice" not in line and "Twin" not in line and "1555" not in line
    resolver = ContactsResolver(storage=storage)
    assert resolver.photo("+15551112222") == JPEG and resolver.photo_count() == 1


def test_hostile_jpeg_headers_are_rejected_quickly() -> None:
    import time

    fill = b"\xff\xd8" + b"\xff" * (limits.MAX_CONTACT_PHOTO_BYTES - 2)
    segments = b"\xff\xd8" + b"\xff\xe0\x00\x02" * ((limits.MAX_CONTACT_PHOTO_BYTES - 2) // 4)
    started = time.perf_counter()
    assert image_dimensions(fill) is None and image_dimensions(segments) is None
    assert image_dimensions(b"\xff\xd8" + b"\xff" * 64 + jpeg(4, 4)[2:]) == (4, 4)
    assert image_dimensions(b"\xff\xd8" + b"\xff" * 65 + jpeg(4, 4)[2:]) is None
    assert time.perf_counter() - started < 1.0


def test_photo_table_creation_does_not_commit_a_failed_replacement(storage, monkeypatch) -> None:
    from blueferry import contact_repository

    ContactRepository(storage).replace([("Old", ["15550001111"], [])])

    class _Spy(sqlite3.Connection):
        """Fail the first statement after the photo table is created."""

        created = False

        def _check(self, sql: str) -> None:
            if self.created:
                raise RuntimeError("interrupted after creating the photo table")
            self.created = "contact_photos (" in sql

        def execute(self, sql, *args):
            self._check(sql)
            return super().execute(sql, *args)

        def executescript(self, sql):
            self._check(sql)
            return super().executescript(sql)

    def spy_open():
        connection = sqlite3.connect(config.CONTACTS_DB, factory=_Spy)
        connection.executescript(contact_repository._SCHEMA)
        return connection

    with monkeypatch.context() as patched:
        patched.setattr(contact_repository, "_open_db", spy_open)
        with pytest.raises(RuntimeError, match="interrupted"):
            ContactRepository(storage).replace(
                [("New", ["15550002222"], [])], photos=[JPEG],
            )

    assert ContactRepository(storage).load() == [("Old", ["15550001111"], [])]
    assert not _photo_table_exists()


def test_photo_cards_parse_the_same_from_bounded_streamed_lines() -> None:
    import io

    from blueferry.vcard import iter_bounded_lines

    blob = (
        _card("FN:Alice", _fold("PHOTO;ENCODING=b:" + _b64(JPEG)), "TEL:+15551112222")
        + _card("FN:Bob", "PHOTO;ENCODING=b:" + _b64(PNG), "EMAIL:bob@example.com")
    )
    expected = _parse_vcard_entries(blob)
    assert [photo for _record, photo in expected] == [JPEG, PNG]
    assert _parse_vcard_entries(iter_bounded_lines(io.StringIO(blob, newline=None))) == expected
    # An unfolded photo longer than the line bound arrives in pieces; the
    # first piece starts the property and the rest are base64 continuations.
    split = _parse_vcard_entries(iter_bounded_lines(io.StringIO(blob, newline=None), limit=64))
    assert [record for record, _photo in split] == [record for record, _photo in expected]
    assert [photo for _record, photo in split] == [JPEG, PNG]


def test_photo_pull_streams_the_transfer_file(storage, monkeypatch, tmp_path) -> None:
    blob = _card("FN:Alice", _fold("PHOTO;ENCODING=b:" + _b64(JPEG)), "TEL:+15551112222")
    _fake_pull(monkeypatch, tmp_path, blob)
    monkeypatch.setattr(config, "CONTACT_PHOTOS", True)

    def no_whole_file_read(*_args, **_kwargs):
        raise AssertionError("the photo path must stream the phonebook")

    with monkeypatch.context() as patched:
        patched.setattr(Path, "read_text", no_whole_file_read)
        assert contacts.pull_phonebook(
            SimpleNamespace(pbap_path="/pbap"), storage=storage,
        ) == 1
    assert ContactsResolver(storage=storage).photo("+15551112222") == JPEG
