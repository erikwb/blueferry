"""Country-wide contact import and lookup contracts, without device or disk I/O."""
from __future__ import annotations

import re

import phonenumbers
import pytest

from blueferry import config
from blueferry.contact_repository import ContactRepository
from blueferry.contacts import ContactsResolver, _parse_vcard_records
from blueferry.events import canonical_address
from blueferry.phone_numbers import address_identities, contact_phone
from blueferry.threads import build_threads, conversation_keys, sort_threads


@pytest.fixture
def resolver_for(monkeypatch):
    def create(records, photos=None):
        entries = list(zip(records, photos or [42] * len(records), strict=True))
        monkeypatch.setattr(ContactRepository, "load_entries", lambda _self, **_kwargs: entries)
        return ContactsResolver(photos=lambda: True)
    return create


def _message(address, index, *, outgoing=False, **extra):
    return {
        "kind": "sms_sent" if outgoing else "sms_received",
        "handle": str(index), "sender_address": address, "body": str(index),
        "timestamp": f"2026-10-20T12:00:{index:02d}Z", **extra,
    }


@pytest.mark.parametrize("region, number", [
    pytest.param(region, number, id=f"{region}-{kind}")
    for region in sorted(phonenumbers.SUPPORTED_REGIONS)
    for kind in (phonenumbers.PhoneNumberType.FIXED_LINE, phonenumbers.PhoneNumberType.MOBILE)
    if (number := phonenumbers.example_number_for_type(region, kind)) is not None
])
def test_country_examples_import_and_match_names_photos_and_conversations(
    monkeypatch, resolver_for, region, number,
):
    monkeypatch.setattr(config, "PHONE_REGION", region)
    national = phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.NATIONAL)
    international = phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)
    national_digits = re.sub(r"\D", "", national)
    for saved in (national, international):
        records = _parse_vcard_records(f"BEGIN:VCARD\nFN:Example\nTEL:{saved}\nEND:VCARD\n")
        destination = international if saved == international else national_digits
        assert records == [("Example", [destination], [])]
        resolver = resolver_for(records)
        assert resolver.find_by_name("Example") == [("Example", destination)]
        assert resolver.count() == 1
        for spelling in (national, national_digits, international):
            assert resolver.resolve(spelling) == "Example"
            assert resolver.photo_ref(spelling) == 42
            assert resolver.thread_addresses(spelling)
        [thread] = build_threads([
            _message(national_digits, 0, outgoing=True), _message(international, 1),
        ], resolver)
        assert thread["name"] == "Example"
        assert thread["recipients"] == [international]
        assert len(thread["messages"]) == 2


@pytest.mark.parametrize("region, saved, spelling", [
    ("GB", "+442079460018", "00442079460018"),
    ("US", "+442079460018", "011442079460018"),
    ("AU", "+442079460018", "0011442079460018"),
    ("JP", "+442079460018", "010442079460018"),
    ("BR", "+5511961234567", "011961234567"),
])
def test_dialling_spellings_share_photos_threads_and_keep_old_keys(
    monkeypatch, resolver_for, region, saved, spelling,
):
    monkeypatch.setattr(config, "PHONE_REGION", region)
    resolver = resolver_for([("Example", [saved], [])])
    assert resolver.resolve(spelling) == "Example"
    assert resolver.photo_ref(spelling) == 42
    assert resolver.thread_addresses(spelling) == resolver.thread_addresses(saved)
    events = [_message(spelling, 0, outgoing=True), _message(saved, 1)]
    [thread] = build_threads(events, resolver)
    assert thread["recipients"] == [saved]
    assert len(thread["messages"]) == 2
    assert build_threads(list(reversed(events)), resolver) == [thread]
    old_key = f"address:{canonical_address(spelling)}"
    assert old_key in conversation_keys(thread)
    assert sort_threads([thread], starred_keys={old_key})[0]["starred"]


def test_slash_formatted_national_contact_preserves_its_delivery_number(monkeypatch, resolver_for):
    monkeypatch.setattr(config, "PHONE_REGION", "SK")
    records = _parse_vcard_records("BEGIN:VCARD\nFN:Example\nTEL:02/21234567\nEND:VCARD\n")
    assert records == [("Example", ["0221234567"], [])]
    resolver = resolver_for(records)
    for spelling in ("02/21234567", "0221234567", "+421221234567"):
        assert resolver.resolve(spelling) == "Example"
        assert resolver.photo_ref(spelling) == 42
        assert resolver.thread_addresses(spelling) == resolver.thread_addresses("0221234567")


def test_shared_spelling_cannot_fall_through_to_a_unique_alias(monkeypatch, resolver_for):
    monkeypatch.setattr(config, "PHONE_REGION", "US")
    resolver = resolver_for([
        ("US Example", ["4582345678"], []),
        ("Danish Example", ["+4582345678"], []),
    ], [41, 42])
    for spelling in ("4582345678", "+14582345678", "01114582345678"):
        assert resolver.resolve(spelling) is None
        assert resolver.photo_ref(spelling) is None
        assert resolver.thread_addresses(spelling) == ()


@pytest.mark.parametrize("first_photo", [None, 41])
def test_conflicting_interpretations_do_not_borrow_a_photo_or_thread(
    monkeypatch, resolver_for, first_photo,
):
    monkeypatch.setattr(config, "PHONE_REGION", "AU")
    resolver = resolver_for([
        ("German Example", ["+493012345"], []),
        ("Australian Example", ["+61493012345"], []),
    ], [first_photo, 42])
    # Without +, these digits could name either stored number. A missing photo
    # for one owner must not make the other interpretation appear unambiguous.
    assert resolver.resolve("493012345") is None
    assert resolver.photo_ref("493012345") is None
    assert resolver.thread_addresses("493012345") == ()
    assert resolver.photo_count() == (1 if first_photo is None else 2)


@pytest.mark.parametrize("region, code", [
    ("US", "911"), ("US", "6262"), ("US", "123456"),
    ("AU", "000"), ("AU", "106"), ("GB", "999"),
    ("AD", "112"), ("AD", "000000"), ("NU", "999"),
    (None, "312345"), ("US", "312345"),
])
def test_service_codes_and_unknown_short_numbers_get_no_country_alias(region, code):
    assert contact_phone(code, region) is None
    assert address_identities(code, region) == (f"short:{code}",)


@pytest.mark.parametrize("region", ["AU", "US", "DE", "BR", "IT", "JP", "IN", "ZA"])
def test_foreign_numbers_do_not_acquire_local_aliases(region):
    for foreign in sorted(phonenumbers.SUPPORTED_REGIONS):
        number = phonenumbers.example_number_for_type(foreign, phonenumbers.PhoneNumberType.MOBILE)
        if number is None or number.country_code == phonenumbers.country_code_for_region(region):
            continue
        international = phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)
        assert address_identities(international, region) == (canonical_address(international),)


@pytest.mark.parametrize("region, number, digits, sender", [
    ("NU", "+6837467", "7467", "Shop"),
    ("DK", "+4532232665", "32232665", "Facebook"),
])
@pytest.mark.parametrize("national", [False, True])
def test_alphanumeric_sender_cannot_join_a_national_number(
    monkeypatch, resolver_for, region, number, digits, sender, national,
):
    monkeypatch.setattr(config, "PHONE_REGION", region)
    resolver = resolver_for([("Person", [number], [])])
    address = digits if national else number
    events = [
        _message(address, 0),
        _message(digits, 1, sender_alphanumeric=sender),
    ]
    service, person = build_threads(events, resolver)
    assert service["name"] == sender
    assert service["key"] == f"alphanumeric:{canonical_address(digits)}"
    assert service["aliases"] == []
    assert not service["reply_ready"]
    assert person["name"] == "Person"
    assert person["reply_ready"]
    assert person["recipients"] == [address]
    assert len(service["messages"]) == len(person["messages"]) == 1
    assert conversation_keys(service).isdisjoint(conversation_keys(person))
    assert build_threads(list(reversed(events)), resolver) == [service, person]
    [service_alone] = build_threads(events[1:], resolver)
    assert service_alone["key"] == service["key"]
    assert service_alone["aliases"] == []
