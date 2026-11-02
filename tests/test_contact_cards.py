"""Contact cards: vCard details, sealed storage, ids, paging, operations, CLI."""
from __future__ import annotations

import json
import sqlite3
import unicodedata
from contextlib import closing

import dbus.exceptions
import pytest
from typer.testing import CliRunner

from blueferry import cli_contacts, config, contacts_view
from blueferry.backend_operations import BackendDependencies, BackendOperations
from blueferry.cli import app
from blueferry.client import ContactNotFoundError, _contact_cards_error
from blueferry.contact_details import (
    EMPTY,
    ContactDetails,
    LabeledValue,
    PostalAddress,
    parse_card_details,
)
from blueferry.contact_repository import ContactRepository, PhotoStoreBusy
from blueferry.contacts import ContactsResolver, _parse_vcard_records
from blueferry.errors import InvalidArgumentsError, NotFoundError, NotReadyError
from blueferry.limits import (
    CARD_ID_HEX_CHARS,
    MAX_CONTACT_ADDRESSES_PER_CARD,
    MAX_CONTACT_DETAIL_CHARS,
    MAX_CONTACT_POSTAL_ADDRESSES_PER_CARD,
)
from blueferry.protocol import ERROR_PREFIX
from blueferry.settings_store import SettingsStore
from blueferry.storage_security import StorageSecurity

from .photo_fixtures import jpeg

JPEG = jpeg()

CARD = "\r\n".join([
    "BEGIN:VCARD",
    "VERSION:3.0",
    "FN:Alice Example",
    "N:Example;Alice;;;",
    "ORG:Example Corp;Research",
    "TITLE:Chief\\, Te",
    " ster",
    "TEL;TYPE=CELL;TYPE=VOICE;TYPE=pref:+1 (555) 111-2222",
    "TEL;CELL;PREF:+1 555 222 3333",
    "TEL;TYPE=HOME,FAX:555 333 4444",
    "item1.TEL:+1 555 777 8888",
    "item1.X-ABLabel:_$!<Pager>!$_",
    "item2.TEL:12",
    "item2.X-ABLabel:Boat",
    "TEL;TYPE=IPHONE:+15559990000",
    "EMAIL;TYPE=INTERNET;TYPE=WORK:Alice@Example.com",
    "EMAIL;type=INTERNET:not an address",
    "item3.EMAIL;type=INTERNET:alice@home.example",
    "item3.X-ABLabel:_$!<Home>!$_",
    "ADR;TYPE=HOME:;Apt 4;1 Main St\\nBack door;Springfield;IL;62701;USA",
    "item4.ADR:;;Lake\\;side 2;Zug;;6300;Switzerland",
    "item4.X-ABLabel:Cabin",
    "ADR;TYPE=WORK:;;;;;;",
    "END:VCARD",
    "",
])




def _card_details(card: str = CARD) -> ContactDetails:
    """Details of one card as a sync sees them: unfolded first."""
    details: list[ContactDetails] = []
    _parse_vcard_records(card, details=details)
    [result] = details
    return result


class _Wallet:
    def get_or_create(self, *, allow_prompt: bool, cancellable=None, create=True) -> bytes:
        return b"K" * 32

    def delete(self, *, allow_prompt: bool, cancellable=None) -> bool:
        return True


@pytest.fixture
def storage(isolated_state):
    selected = StorageSecurity(
        settings=SettingsStore(config.SETTINGS_JSON), key_provider=_Wallet(),
    )
    assert selected.status.can_write
    yield selected
    selected.close()


# ---- vCard parsing -----------------------------------------------------------

def test_card_details_keep_labels_values_and_postal_addresses() -> None:
    details = _card_details()
    assert details.organization == "Example Corp, Research"
    assert details.title == "Chief, Tester"
    assert details.phones == (
        LabeledValue("mobile", "", "+1 (555) 111-2222", "+15551112222"),
        LabeledValue("mobile", "", "+1 555 222 3333", "+15552223333"),
        LabeledValue("home_fax", "", "555 333 4444", "5553334444"),
        LabeledValue("pager", "", "+1 555 777 8888", "+15557778888"),
        LabeledValue("custom", "Boat", "12", ""),
        LabeledValue("iphone", "", "+15559990000", "+15559990000"),
    )
    assert details.emails == (
        LabeledValue("work", "", "Alice@Example.com"),
        LabeledValue("home", "", "alice@home.example"),
    )
    assert details.addresses == (
        PostalAddress(
            "home", "", "", "Apt 4", "1 Main St\nBack door", "Springfield", "IL",
            "62701", "USA",
        ),
        PostalAddress("custom", "Cabin", "", "", "Lake;side 2", "Zug", "", "6300", "Switzerland"),
    )


def test_card_details_decode_quoted_printable_vcard_21() -> None:
    card = "\r\n".join([
        "BEGIN:VCARD",
        "VERSION:2.1",
        "FN:Jörg",
        "TEL;CELL;PREF:+1 555 444 5555",
        "ORG;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:B=C3=A4ckerei;M=C3=BChle",
        "TITLE;CHARSET=ISO-8859-1;QUOTED-PRINTABLE:Gesch=E4ftsf=FChrer",
        "ADR;HOME;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:;;Stra=C3=9Fe 1=3B Hof;Z=",
        "=C3=BCrich;;8000;",
        "item1.EMAIL:joerg@example.com",
        "item1.X-ABLabel:_$!<Mobile>!$_",
        "END:VCARD",
        "",
    ])
    details = _card_details(card)
    assert details.organization == "Bäckerei, Mühle"
    assert details.title == "Geschäftsführer"
    assert details.phones[0].kind == "mobile"
    assert details.addresses == (
        PostalAddress("home", "", "", "", "Straße 1; Hof", "Zürich", "", "8000", ""),
    )
    # A built-in label that is no email kind keeps its readable text.
    assert details.emails == (LabeledValue("custom", "Mobile", "joerg@example.com"),)


def test_card_details_are_bounded() -> None:
    lines = ["BEGIN:VCARD", "FN:Many"]
    lines += [f"TEL;TYPE=CELL:+1555000{index:04d}" for index in range(100)]
    lines += [f"ADR;TYPE=HOME:;;{index} Road;Town;;;" for index in range(40)]
    lines += ["ORG:" + "x" * (MAX_CONTACT_DETAIL_CHARS * 2), "END:VCARD"]
    details = parse_card_details("\n".join(lines))
    assert len(details.phones) == MAX_CONTACT_ADDRESSES_PER_CARD
    assert len(details.addresses) == MAX_CONTACT_POSTAL_ADDRESSES_PER_CARD
    assert len(details.organization) == MAX_CONTACT_DETAIL_CHARS


def test_parser_collects_details_aligned_with_records() -> None:
    other = "BEGIN:VCARD\nFN:Bob\nTEL:+15550001111\nEND:VCARD\n"
    nothing = "BEGIN:VCARD\nNOTE:no name or address\nEND:VCARD\n"
    details: list[ContactDetails] = []
    records = _parse_vcard_records(CARD + nothing + other, details=details)
    assert [record[0] for record in records] == ["Alice Example", "Bob"]
    assert [item.organization for item in details] == ["Example Corp, Research", ""]
    assert details[1].phones == (LabeledValue("other", "", "+15550001111", "+15550001111"),)


def test_storage_form_round_trips_and_rejects_malformed_rows() -> None:
    details = _card_details()
    assert ContactDetails.from_storage(json.loads(json.dumps(details.to_storage()))) == details
    assert ContactDetails.from_storage(None) is EMPTY
    assert not EMPTY and EMPTY.to_storage() == {}
    broken = ContactDetails.from_storage({
        "phones": [["mobile", "", "+1555"], ["satellite", "", "1", ""], 7,
                   ["mobile", "", "+15551112222", "+15551112222"]],
        "emails": [["work", "", "nope"], ["work", "", "a@example.com"]],
        "addresses": [["home", ""] + [""] * 7, ["home", "", "", "", "Road"]],
        "organization": 5,
    })
    assert broken.phones == (LabeledValue("mobile", "", "+15551112222", "+15551112222"),)
    assert broken.emails == (LabeledValue("work", "", "a@example.com"),)
    assert broken.addresses == () and broken.organization == ""


# ---- repository and resolver ---------------------------------------------------

def _store(storage, *, photos=None) -> list[ContactDetails]:
    details: list[ContactDetails] = []
    records = _parse_vcard_records(
        CARD + "BEGIN:VCARD\nFN:Bob Other\nTEL:+15550001111\nEND:VCARD\n", details=details,
    )
    ContactRepository(storage).replace(records, photos=photos, details=details)
    return details


def test_details_are_sealed_and_never_in_plaintext(storage) -> None:
    _store(storage)
    raw = config.CONTACTS_DB.read_bytes()
    for secret in (b"Springfield", b"Example Corp", b"Boat", b"Alice"):
        assert secret not in raw
    with closing(sqlite3.connect(config.CONTACTS_DB)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 0
    [alice, bob] = ContactRepository(storage).load_cards()
    assert alice[2].organization == "Example Corp, Research"
    assert bob[2].phones[0].value == "+15550001111"


def test_plaintext_cache_keeps_no_details(isolated_state) -> None:
    ContactRepository().replace(
        [("Alice", ["15551112222"], [])], details=[_card_details()],
    )
    [(record, photo, details)] = ContactRepository().load_cards()
    assert record == ("Alice", ["15551112222"], []) and photo is None and details is EMPTY


def test_cards_page_query_and_keep_ids_across_unchanged_resyncs(storage) -> None:
    _store(storage)
    resolver = ContactsResolver(storage=storage)
    assert resolver.card_count() == 2
    total, page = resolver.cards()
    assert total == 2 and [view[1][0] for view in page] == ["Alice Example", "Bob Other"]
    alice_id = page[0][0]
    assert resolver.card(alice_id)[2].title == "Chief, Tester"
    assert resolver.cards(1, 5)[1][0][1][0] == "Bob Other"
    assert resolver.cards(0, 1)[0] == 2
    # Name, organization, mail, postal text, and phone digits in any spelling.
    for query in ("alice", "EXAMPLE CORP", "home.example", "springfield", "(555) 777",
                  "5557778888", "  research  "):
        assert [view[0] for view in resolver.cards(query=query)[1]] == [alice_id], query
    assert resolver.cards(query="55")[0] == 0  # too short to be a number query
    assert resolver.cards(query="nobody")[1] == []

    _store(storage)
    resolver.refresh()
    assert resolver.card(alice_id) is not None
    ContactRepository(storage).replace([("Alice Example", ["15551112222"], [])])
    resolver.refresh()
    assert resolver.card(alice_id) is None


def test_duplicate_cards_get_distinct_ids(storage) -> None:
    ContactRepository(storage).replace([("Twin", ["15551112222"], [])] * 2)
    ids = [view[0] for view in ContactsResolver(storage=storage).cards()[1]]
    assert len(set(ids)) == 2 and ids[1] == f"{ids[0]}-2"


def test_card_photo_follows_the_photo_setting(storage) -> None:
    _store(storage, photos=[JPEG, None])
    with_photos = ContactsResolver(storage=storage, photos=lambda: True)
    view = with_photos.cards()[1][0]
    assert view[3] is not None and with_photos.load_photo(view[3]) == JPEG
    without = ContactsResolver(storage=storage, photos=lambda: False)
    assert without.cards()[1][0][3] is None


def test_snapshot_keeps_cards() -> None:
    resolver = ContactsResolver.__new__(ContactsResolver)
    resolver.storage = None
    resolver._photos = lambda: False
    resolver._repository = type("R", (), {
        "load_cards": staticmethod(lambda **_kwargs: [
            (("Alice", ["15551112222"], []), None, _card_details()),
        ]),
    })()
    resolver._mem, resolver._records, resolver.photo_revision = {}, [], 0
    resolver._warm()
    copy = resolver.snapshot()
    assert copy.cards(query="research")[0] == 1
    assert copy.card(resolver.cards()[1][0][0]) is not None


# ---- operations ------------------------------------------------------------------

class _Sessions:
    map = object()
    pbap = object()
    map_path = "/session/map"

    @staticmethod
    def report_error(_error):
        pass


def _operations(storage, *, photos=False) -> BackendOperations:
    resolver = ContactsResolver(storage=storage, photos=lambda: photos)
    return BackendOperations(
        _Sessions(), BackendDependencies(contacts=resolver, contact_photos=photos),
    )


def test_list_contact_cards_pages_without_photo_bytes(storage) -> None:
    _store(storage, photos=[JPEG, None])
    operations = _operations(storage, photos=True)
    page = operations.list_contact_cards(0, 1, "")
    assert page["total"] == 2 and page["offset"] == 0 and page["query"] == ""
    [row] = page["contacts"]
    assert row == {
        "id": row["id"], "name": "Alice Example", "organization": "Example Corp, Research",
        "primary": "+1 (555) 111-2222", "phone_count": 6, "email_count": 2,
        "address_count": 2, "has_photo": True,
    }
    assert "photo" not in json.dumps(page).replace("has_photo", "")
    assert operations.list_contact_cards(1, 10, "")["contacts"][0]["has_photo"] is False
    assert operations.list_contact_cards(0, 10, " bob   other ")["query"] == "bob other"
    status = operations.status()
    assert status["contact_card_count"] == 2
    epoch = status["contact_card_epoch"]
    assert isinstance(epoch, int) and not isinstance(epoch, bool)
    operations.dependencies.contacts.refresh()
    assert operations.status()["contact_card_epoch"] != epoch
    with pytest.raises(InvalidArgumentsError):
        operations.list_contact_cards(0, 10, "x" * 300)


def test_get_contact_returns_details_and_photo(storage) -> None:
    _store(storage, photos=[JPEG, None])
    operations = _operations(storage, photos=True)
    alice_id = operations.list_contact_cards(0, 1, "")["contacts"][0]["id"]
    document, photo = operations.contact_card(alice_id)
    assert photo == JPEG and document["has_photo"] is True and document["photo_retry"] is False
    assert document["title"] == "Chief, Tester"
    assert document["phones"][0] == {
        "kind": "mobile", "label": "", "value": "+1 (555) 111-2222", "number": "+15551112222",
    }
    assert document["emails"][1] == {"kind": "home", "label": "", "value": "alice@home.example"}
    assert document["addresses"][1]["label"] == "Cabin"
    assert document["addresses"][0]["street"] == "1 Main St\nBack door"
    with pytest.raises(NotFoundError):
        operations.contact_card("0" * 18)
    with pytest.raises(InvalidArgumentsError):
        operations.contact_card(" ")
    # Photos off: the same card, no bytes, no photo flag.
    document, photo = _operations(storage).contact_card(alice_id)
    assert photo == b"" and document["has_photo"] is False


def test_get_contact_reports_a_busy_photo_store_as_retry(storage, monkeypatch) -> None:
    _store(storage, photos=[JPEG, None])
    operations = _operations(storage, photos=True)
    alice_id = operations.list_contact_cards(0, 1, "")["contacts"][0]["id"]

    def busy(_self, _ref):
        raise PhotoStoreBusy("locked")

    monkeypatch.setattr(ContactsResolver, "load_photo", busy)
    document, photo = operations.contact_card(alice_id)
    assert photo == b"" and document["photo_retry"] is True and document["has_photo"] is True


def test_legacy_cards_fall_back_to_the_messaging_addresses(storage) -> None:
    ContactRepository(storage).replace([("Old", ["15551112222"], ["old@example.com"])])
    operations = _operations(storage)
    old_id = operations.list_contact_cards(0, 5, "")["contacts"][0]["id"]
    document, _photo = operations.contact_card(old_id)
    assert document["phones"] == [
        {"kind": "other", "label": "", "value": "15551112222", "number": "15551112222"},
    ]
    assert document["emails"] == [{"kind": "other", "label": "", "value": "old@example.com"}]
    assert document["addresses"] == []


def test_card_methods_need_a_contact_cache() -> None:
    operations = BackendOperations(_Sessions())
    with pytest.raises(NotReadyError):
        operations.list_contact_cards(0, 5, "")
    with pytest.raises(NotReadyError):
        operations.contact_card("0" * 18)
    assert "contact_card_count" not in operations.status()


# ---- CLI -----------------------------------------------------------------------

class _Client:
    def __init__(self, cards, document=None, photo=b""):
        self.cards, self.document, self.photo = cards, document, photo
        self.queries: list[tuple[int, int, str]] = []

    def contact_cards(self, offset=0, limit=100, query=""):
        self.queries.append((offset, limit, query))
        rows = [card for card in self.cards if query.casefold() in card["name"].casefold()]
        return {"contacts": rows[offset:offset + limit], "total": len(rows),
                "offset": offset, "query": query}

    def contact(self, contact_id):
        if self.document is None:
            raise ContactNotFoundError("contact is no longer in the contact cache")
        return self.document, self.photo


def _row(card_id, name, **extra):
    return {"id": card_id, "name": name, "organization": "", "primary": "+15551112222",
            "phone_count": 1, "email_count": 0, "address_count": 0, "has_photo": False,
            **extra}


def test_cli_lists_cards_with_escaped_names(monkeypatch) -> None:
    client = _Client([_row("a" * 18, "Alice\x1b[31m"), _row("b" * 18, "Bob")])
    monkeypatch.setattr(cli_contacts, "BackendClient", lambda: client)
    result = CliRunner().invoke(app, ["contacts", "list", "--limit", "1", "--all"])
    assert result.exit_code == 0, result.output
    assert "aaaaaaaaaaaaaaaaaa  Alice" in result.output and "\x1b[31m" not in result.output
    assert "Bob" in result.output and "2 contacts" in result.output
    assert client.queries == [(0, 1, ""), (1, 1, "")]
    empty = CliRunner().invoke(app, ["contacts", "list", "-q", "zed"])
    assert "No contact matches" in empty.output


def test_cli_shows_a_card_by_name_and_writes_the_photo(monkeypatch, tmp_path) -> None:
    document = {
        "id": "a" * 18, "name": "Alice", "organization": "Corp", "title": "Boss",
        "phones": [{"kind": "mobile", "label": "", "value": "+1 555", "number": "+1555"}],
        "emails": [{"kind": "custom", "label": "Club", "value": "a@example.com"}],
        "addresses": [{"kind": "home", "label": "", "street": "1 Road", "locality": "Zug",
                       "postal_code": "6300"}],
        "has_photo": True, "photo_retry": False,
    }
    client = _Client([_row("a" * 18, "Alice"), _row("b" * 18, "Bobbie")], document, JPEG)
    monkeypatch.setattr(cli_contacts, "BackendClient", lambda: client)
    target = tmp_path / "alice.jpg"
    result = CliRunner().invoke(app, ["contacts", "show", "ali", "--photo", str(target)])
    assert result.exit_code == 0, result.output
    assert "Boss · Corp" in result.output
    assert "Mobile: +1 555" in result.output and "Club: a@example.com" in result.output
    assert "1 Road" in result.output and "6300 Zug" in result.output
    assert target.read_bytes() == JPEG
    ambiguous = CliRunner().invoke(app, ["contacts", "show", "e"])
    assert ambiguous.exit_code == 2 and "use an id" in ambiguous.output
    missing = CliRunner().invoke(app, ["contacts", "show", "zed"])
    assert missing.exit_code == 1


def test_cli_reports_a_stale_card_id_by_its_error_name(monkeypatch) -> None:
    client = _Client([])
    monkeypatch.setattr(cli_contacts, "BackendClient", lambda: client)
    result = CliRunner().invoke(app, ["contacts", "show", "c" * CARD_ID_HEX_CHARS])
    assert result.exit_code == 1 and contacts_view.stale_text() in result.output


def test_client_maps_the_not_found_error_name() -> None:
    stale = dbus.exceptions.DBusException("gone", name=f"{ERROR_PREFIX}.NotFound")
    assert isinstance(_contact_cards_error(stale), ContactNotFoundError)
    other = dbus.exceptions.DBusException("NotFound", name=f"{ERROR_PREFIX}.Failed")
    assert not isinstance(_contact_cards_error(other), ContactNotFoundError)


def test_card_ids_have_the_shared_length(storage) -> None:
    _store(storage)
    [first, _second] = ContactsResolver(storage=storage).cards()[1]
    assert len(first[0]) == CARD_ID_HEX_CHARS and cli_contacts._CARD_ID.fullmatch(first[0])


@pytest.mark.parametrize("query", ["Müller", "Muller", "MULLER", "müller"])
def test_card_query_ignores_accents_and_unicode_forms(storage, query) -> None:
    stored = unicodedata.normalize("NFD", "Jörg Müller")
    ContactRepository(storage).replace([(stored, ["15551112222"], [])])
    resolver = ContactsResolver(storage=storage)
    for spelling in (query, unicodedata.normalize("NFD", query)):
        assert resolver.cards(query=spelling)[0] == 1, spelling


def test_a_query_that_folds_away_matches_nothing(storage) -> None:
    ContactRepository(storage).replace([("Alice", ["15551112222"], [])])
    resolver = ContactsResolver(storage=storage)
    assert resolver.cards(query="\u0301\u0308")[0] == 0
    assert resolver.cards(query="  ")[0] == 1
