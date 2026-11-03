"""The toolkit-neutral presenter for the contact list and contact card."""
from __future__ import annotations

import json

from blueferry import contacts_view as view

PAGE = {
    "contacts": [
        {"id": "a1", "name": "Alice Example", "organization": "Corp",
         "primary": "+1 555", "phone_count": 2, "email_count": 1,
         "address_count": 0, "has_photo": True},
        {"id": "a1", "name": "Duplicate id"},
        {"id": "b2", "name": "", "organization": "", "primary": "+1 777"},
        {"name": "No id"},
        "junk",
    ],
    "total": 5, "offset": 0, "query": "",
}

CARD = {
    "id": "a1", "name": "Alice Example", "organization": "Corp", "title": "Boss",
    "phones": [
        {"kind": "mobile", "label": "", "value": "+1 (555) 111", "number": "+1555111"},
        {"kind": "custom", "label": "Boat", "value": "12", "number": ""},
        {"kind": "mobile", "label": "", "value": ""},
    ],
    "emails": [{"kind": "work", "label": "", "value": "a@example.com"}],
    "addresses": [
        {"kind": "home", "label": "", "street": "1 Main St\nBack", "locality": "Zug",
         "postal_code": "6300", "country": "Switzerland"},
        {"kind": "work", "label": ""},
    ],
    "has_photo": True, "photo_retry": False,
}


def test_list_rows_paging_and_capability() -> None:
    rows = view.list_rows(json.dumps(PAGE))
    assert rows == [
        {"id": "a1", "title": "Alice Example", "subtitle": "Corp · +1 555",
         "initials": "AE", "has_photo": True},
        {"id": "b2", "title": "+1 777", "subtitle": "", "initials": "", "has_photo": False},
    ]
    assert view.has_more(PAGE) and view.next_offset(PAGE) == 2
    listed = view.list_view(PAGE)
    assert listed["count"] == 2 and listed["count_text"] == "5 contacts" and listed["hint"] == ""
    assert view.supported({"contact_card_count": 0})
    assert not view.supported({}) and not view.supported({"contact_card_count": True})


def test_empty_hints_and_counts() -> None:
    assert "Sync Contacts" in view.empty_hint({"contacts": [], "total": 0})
    assert "“zed”" in view.empty_hint({"contacts": [], "total": 0, "query": "zed"})
    assert view.count_text({"contacts": [], "total": 1, "query": "a"}) == "1 matching contact"
    assert view.normalize_page("not json")["contacts"] == []


def test_card_sections_labels_and_actions() -> None:
    card = view.card_view(CARD)
    assert card["title"] == "Alice Example" and card["subtitle"] == "Boss · Corp"
    assert card["initials"] == "AE" and card["has_photo"] and not card["photo_retry"]
    phones, emails, addresses = card["sections"]
    assert phones["id"] == view.SECTION_PHONES and len(phones["rows"]) == 2
    assert phones["rows"][0] == {
        "label": "Mobile", "value": "+1 (555) 111", "target": "+1555111",
        "actions": [view.ACTION_DIAL, view.ACTION_MESSAGE, view.ACTION_COPY],
    }
    assert phones["rows"][1]["label"] == "Boat"
    assert phones["rows"][1]["actions"] == [view.ACTION_COPY]
    assert emails["rows"][0]["label"] == "Work"
    assert emails["rows"][0]["actions"][0] == view.ACTION_MAIL
    [address] = addresses["rows"]
    assert address["value"] == "1 Main St\nBack\n6300 Zug\nSwitzerland"
    assert view.kind_label("satellite") == "Other" and view.kind_label("home_fax") == "Home fax"


def test_card_without_name_or_details() -> None:
    assert view.card_view({})["title"] == "No name"
    assert view.card_view({})["sections"] == []
    assert view.card_header({"organization": "Corp"})["title"] == "Corp"
    assert view.stale_text() and view.load_failed_text("") == "Could not load contacts."
