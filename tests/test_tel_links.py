"""Phone links from other applications only ever yield a number to confirm."""
from __future__ import annotations

import pytest

from blueferry.calls.model import normalize_dial_number
from blueferry.conversation_state import ConversationSnapshot, ConversationState
from blueferry.models import Thread
from blueferry.tel_links import (
    MAX_PHONE_LINK_CHARS,
    dial_request,
    is_emergency_number,
    known_contact_name,
    parse_phone_link,
    phone_link_argument,
)


@pytest.mark.parametrize(("link", "number"), [
    ("tel:+41790000000", "+41790000000"),
    ("tel:+41 79 000 00 00", "+41790000000"),
    ("TEL:+41-79-000-00-00", "+41790000000"),
    ("Tel:+1.555.010.0000", "+15550100000"),
    ("tel:+1 (555) 010-0000", "+15550100000"),
    ("tel:+41/79/000 00 00", "+41790000000"),
    ("tel://+41790000000", "+41790000000"),
    ("callto:+41790000000", "+41790000000"),
    ("CALLTO://+41 79 000 00 00/", "+41790000000"),
    # Firefox and Thunderbird hand the link over percent-encoded.
    ("tel:+41%2079%20000%2000%2000", "+41790000000"),
    ("tel:%2B41790000000", "+41790000000"),
    ("tel:+41\u00a079\u00a0000", "+4179000"),
    # Significant zeroes must be preserved, including Italy's leading zero.
    ("tel:+39 06 6982", "+39066982"),
    ("tel:+39 (06) 6982", "+39066982"),
    # Away from a possible country code, "(0)" is part of the number.
    ("tel:+41 79 (0) 000", "+41790000"),
    ("tel:044 (0) 000", "0440000"),
    # Unknown optional parameters may be ignored (RFC 3966 section 5.4).
    ("tel:+41790000000;label=office", "+41790000000"),
    ("tel:+41790000000;label=office%20phone;flag", "+41790000000"),
    ("callto:+41790000000;label=office", "+41790000000"),
    ("callto:+41790000000?call", "+41790000000"),
    ("  tel:+41790000000\n", "+41790000000"),
    # Plain numbers, as for ``--dial``.
    ("0790000000", "0790000000"),
    ("+41 79 000 00 00", "+41790000000"),
    ("5", "5"),
    ("tel:" + "1" * 32, "1" * 32),
])
def test_valid_links_yield_the_dialable_number(link, number):
    assert parse_phone_link(link) == number
    # The daemon accepts exactly what the clients offer to call.
    assert normalize_dial_number(number) == number


@pytest.mark.parametrize("link", [
    "",
    "   ",
    "tel:",
    "tel://",
    "callto:",
    "tel:+",
    "tel:;ext=1",
    "tel:+41+79",
    "tel:41+79",
    "tel:++41790000000",
    # Service codes reconfigure the phone and the daemon refuses them.
    "tel:*21*0790000000#",
    "tel:%2A%2321%23",
    "tel:*#06#",
    "tel:0790000000#",
    "tel:abc",
    "tel:+41 79 CALL NOW",
    "callto:someone.example",
    "callto://user@example.com",
    "sms:+41790000000",
    "https://example.com/?tel=+41790000000",
    "mailto:+41790000000",
    "tel:+41%3B790000000",
    "tel:+4179%0A0000000",
    "tel:+4179%000000000",
    "tel:+41\t79",
    "tel:%FF41790000000",
    "tel:%2541",
    "tel:\uff14\uff11",  # full-width digits
    "tel:\u0664\u0661",  # Arabic-Indic digits
    "tel:" + "1" * 33,
    "tel:+" + "1" * 33,
])
def test_anything_else_is_refused(link):
    assert parse_phone_link(link) is None
    assert dial_request(link) == ("", "invalid")


@pytest.mark.parametrize("link", [
    "tel:+41 (0)79 000 00 00", "tel:+41(0)790000000", "tel:+49 (0) 30 0000000",
    "tel:+41%20(0)79%20000%2000%2000", "tel:+41%28%30%29790000000",
    "+1 (0) 555 010 0000", "callto:+39(0)66982", "tel:+39 (0)6 6982",
])
def test_ambiguous_trunk_prefixes_are_refused_instead_of_rewriting_digits(link):
    assert dial_request(link) == ("", "invalid")


@pytest.mark.parametrize("scheme", ["tel:", "CALLTO://", ""])
@pytest.mark.parametrize("subscriber", [
    # A context identifies a numbering plan; it is not a dialable prefix.
    "7042;phone-context=example.com",
    "863-1234;phone-context=+1-914-555",
    "+15550100000;PHONE-CONTEXT=example.com",
    # Silently dropping an extension or subaddress changes the destination.
    "+15550100000;ext=123",
    "+15550100000;EXT=123",
    "+15550100000;isub=1",
    "+15550100000;postd=pp22",
    "+15550100000;m-routing=private",
    "+15550100000;M-routing=private",
    "+15550100000;label=office;m-routing=private",
    "+15550100000?phone-context=example.com",
    "+15550100000?call;phone-context=example.com",
    "+15550100000?call&ext=123",
    "+15550100000;phone-context=example.com?call",
])
def test_unsupported_dialing_instructions_are_refused(scheme, subscriber):
    assert dial_request(scheme + subscriber) == ("", "invalid")


@pytest.mark.parametrize("suffix", [
    ";", ";;label=office", ";label=", ";=office", ";label=office;LABEL=home",
    ";phone%2Dcontext=example.com", ";%6d-routing=private", ";label=%",
    ";label=%GG", ";label=office#fragment", ";label=office\nphone",
    ";label=office?ignored", "?call", "?", "?ignored",
])
def test_malformed_parameters_and_tel_queries_are_refused(suffix):
    assert dial_request("tel:+15550100000" + suffix) == ("", "invalid")


@pytest.mark.parametrize("value", [None, 41790000000, b"tel:+41790000000", ["tel:1"]])
def test_non_text_is_refused(value):
    assert parse_phone_link(value) is None


def test_overlong_input_is_refused_before_parsing():
    padded = "tel:+41790000000;" + "x" * MAX_PHONE_LINK_CHARS
    assert parse_phone_link(padded[:MAX_PHONE_LINK_CHARS]) == "+41790000000"
    assert parse_phone_link(padded) is None
    assert parse_phone_link(" " * MAX_PHONE_LINK_CHARS + "1") is None


@pytest.mark.parametrize("link", ["tel:112", "tel:+112", "tel:9-1-1", "callto:144", "117"])
def test_emergency_numbers_are_reported_not_dialed(link):
    number, problem = dial_request(link)
    assert problem == "emergency"
    assert is_emergency_number(number)


def test_numbers_containing_an_emergency_number_are_ordinary():
    assert dial_request("tel:1120") == ("1120", "")
    assert dial_request("tel:+41 79 000 01 12") == ("+41790000112", "")


def _thread(key, name, *, aliases=(), is_group=False):
    value = Thread(
        key=key, name=name, is_group=is_group, recipients=(), reply_ready=True,
        messages=(), last_ts="",
    ).to_dict()
    value["aliases"] = list(aliases)
    return Thread.from_dict(value)


def _state(*threads):
    state = ConversationState(select_first=False)
    state.apply_snapshot(ConversationSnapshot(threads=threads))
    return state


def test_contact_name_comes_from_known_conversations():
    state = _state(
        _thread("address:phone:41790000000", "Test Contact"),
        _thread(
            "address:phone:15550100000", "Second Contact",
            aliases=("address:phone:15550100001",),
        ),
        _thread("address:phone:15550100002", "+1 555 010 0002"),
        _thread("group:named:x", "Group", aliases=("address:phone:15550100003",), is_group=True),
    )
    assert known_contact_name(state, "+41790000000") == "Test Contact"
    # Another address of the same synced contact.
    assert known_contact_name(state, "+15550100001") == "Second Contact"
    # A conversation without a contact is named after its number.
    assert known_contact_name(state, "+15550100002") == ""
    assert known_contact_name(state, "+15550100003") == ""
    assert known_contact_name(state, "+15550100009") == ""
    assert known_contact_name(state, "") == ""
    assert known_contact_name(_state(), "+41790000000") == ""


def test_only_scheme_arguments_are_taken_as_links():
    assert phone_link_argument([]) == ("", [])
    assert phone_link_argument(["-platform", "wayland", "tel:+15550100000"]) == (
        "tel:+15550100000", ["-platform", "wayland"],
    )
    assert phone_link_argument(["CallTo:+1", "tel:+2"]) == ("CallTo:+1", ["tel:+2"])
    # A bare number or another URI stays a toolkit argument.
    assert phone_link_argument(["0790000000", "sms:1"]) == ("", ["0790000000", "sms:1"])
