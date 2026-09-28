"""Pure oFono parsing, modem selection, and call-input validation.

These tests call pure functions only; the harness forbids every real bus.
"""
from __future__ import annotations

import dbus
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from blueferry.calls.model import (
    MAX_DIAL_DIGITS,
    VOICE_CALL_MANAGER_IFACE,
    ModemInfo,
    call_id_from_path,
    normalize_dial_number,
    parse_call,
    parse_line_identification,
    parse_modem,
    remote_text,
    select_modem,
    validate_call_id,
    validate_dtmf,
)
from blueferry.errors import InvalidArgumentsError
from blueferry.limits import MAX_REMOTE_PROPERTY_CHARS

PROPERTY_SETTINGS = settings(max_examples=150, derandomize=True, deadline=None)
MAC = "AA:BB:CC:DD:EE:FF"
PHONE_PATH = "/hfp/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"

_ANY_VALUE = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=True)
    | st.text(max_size=300) | st.binary(max_size=32),
    lambda children: st.lists(children, max_size=4)
    | st.dictionaries(st.text(max_size=16), children, max_size=4),
    max_leaves=12,
)
_PROPERTY_NAMES = st.sampled_from([
    "State", "LineIdentification", "Name", "Multiparty", "Emergency",
    "Powered", "Online", "Interfaces", "Type", "Other",
])


@pytest.mark.parametrize("raw,expected", [
    ("+41 79 123 45 67", "+41791234567"),
    ("(555) 123-4567", "5551234567"),
    ("112", "112"),
])
def test_dial_numbers_are_normalized(raw, expected) -> None:
    assert normalize_dial_number(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "   ", "abc", "1+2", "++41", "0800;ATH", "123\r\nATD", "12p34", "12w", None, 42,
    "1" * (MAX_DIAL_DIGITS + 1),
])
def test_dial_numbers_reject_anything_else(raw) -> None:
    with pytest.raises(InvalidArgumentsError):
        normalize_dial_number(raw)


@pytest.mark.parametrize("raw", ["**21*0791234567#", "##002#", "*#06#", "*31#0800123", "123#"])
def test_service_codes_are_never_dialed(raw) -> None:
    with pytest.raises(InvalidArgumentsError, match="service codes are not allowed"):
        normalize_dial_number(raw)


def test_keypad_symbols_remain_available_as_dtmf() -> None:
    assert validate_dtmf("*21#") == "*21#"


@pytest.mark.parametrize("raw", ["1", "0123456789*#", " 5 "])
def test_dtmf_accepts_only_keypad_tones(raw) -> None:
    assert validate_dtmf(raw) == raw.strip()


@pytest.mark.parametrize("raw", ["", "A", "1 2", "12,", "p", "1" * 33, None, 5])
def test_dtmf_rejects_non_keypad_input(raw) -> None:
    with pytest.raises(InvalidArgumentsError):
        validate_dtmf(raw)


@pytest.mark.parametrize("raw", ["", "../x", "a b", "x" * 65, "call/1", None])
def test_client_call_ids_are_strict(raw) -> None:
    with pytest.raises(InvalidArgumentsError):
        validate_call_id(raw)


def test_call_id_is_the_last_path_segment() -> None:
    assert call_id_from_path(f"{PHONE_PATH}/voicecall01") == "voicecall01"
    assert call_id_from_path("not a path") is None
    assert call_id_from_path(42) is None


def test_modem_selection_is_bound_to_the_configured_phone() -> None:
    other_phone = ModemInfo("/hfp/org/bluez/hci0/dev_11_22_33_44_55_66", "hfp", True, True)
    wrong_type = ModemInfo(PHONE_PATH, "hardware", True, True)

    assert select_modem([other_phone, wrong_type], mac=MAC, adapter="hci0") is None


def test_modem_selection_prefers_the_configured_adapter_then_online() -> None:
    online_elsewhere = ModemInfo(
        "/hfp/org/bluez/hci1/dev_AA_BB_CC_DD_EE_FF", "hfp", True, True,
        frozenset({VOICE_CALL_MANAGER_IFACE}),
    )
    offline_here = ModemInfo(PHONE_PATH, "hfp", False, False)
    online_here = ModemInfo(
        "/hfp/org/bluez/hci0/x/dev_AA_BB_CC_DD_EE_FF", "hfp", True, True,
    )

    assert select_modem(
        [online_elsewhere, offline_here], mac=MAC, adapter="hci0",
    ) == offline_here
    assert select_modem(
        [offline_here, online_here], mac=MAC, adapter="hci0",
    ) == online_here


def test_modem_properties_track_powered_online_and_interfaces() -> None:
    modem = parse_modem(dbus.ObjectPath(PHONE_PATH), dbus.Dictionary({
        "Type": dbus.String("hfp"),
        "Powered": dbus.Boolean(False),
        "Online": dbus.Boolean(False),
    }, signature="sv"))
    assert modem is not None and not modem.powered and not modem.voice_ready

    modem = modem.updated("Powered", dbus.Boolean(True))
    modem = modem.updated("Online", dbus.Boolean(True))
    assert modem.powered and modem.online and not modem.voice_ready
    modem = modem.updated(
        "Interfaces", dbus.Array([VOICE_CALL_MANAGER_IFACE], signature="s"),
    )
    assert modem.voice_ready


def test_integer_is_not_a_powered_boolean() -> None:
    modem = parse_modem(PHONE_PATH, {"Type": "hfp", "Powered": 1, "Online": 1})

    assert modem is not None and not modem.powered and not modem.online


def test_incoming_call_is_parsed_with_direction_and_safe_display_text() -> None:
    record = parse_call(f"{PHONE_PATH}/voicecall01", {
        "State": "incoming",
        "LineIdentification": "+41 79 123 45 67",
        "Name": "Evil\x1b[31m\nName",
    })

    assert record is not None
    assert record.direction == "incoming"
    assert record.ringing
    assert record.number == "+41791234567"
    assert record.network_name == "Evil [31m Name"
    wire = record.to_wire()
    assert "path" not in wire
    assert wire["call_id"] == "voicecall01"


def test_withheld_or_textual_caller_id_is_not_a_number() -> None:
    assert parse_line_identification("withheld") == ""
    assert parse_line_identification("") == ""


def test_unknown_state_is_normalized() -> None:
    record = parse_call(f"{PHONE_PATH}/voicecall02", {"State": "exploding"})

    assert record is not None and record.state == "unknown"
    assert record.direction == "unknown"


@PROPERTY_SETTINGS
@given(st.dictionaries(_PROPERTY_NAMES, _ANY_VALUE, max_size=8), _ANY_VALUE)
def test_arbitrary_ofono_properties_cannot_crash_or_grow_unbounded(props, path) -> None:
    modem = parse_modem(path, props)
    call = parse_call(path, props)
    for name, value in props.items():
        if modem is not None:
            modem = modem.updated(name, value)
        if call is not None:
            call = call.with_property(name, value)
    if call is not None:
        wire = call.to_wire()
        assert all(
            len(value) <= MAX_REMOTE_PROPERTY_CHARS
            for value in wire.values() if isinstance(value, str)
        )
        assert call.state in {
            "active", "held", "dialing", "alerting", "incoming", "waiting",
            "disconnected", "unknown",
        }
    if modem is not None:
        assert isinstance(modem.powered, bool) and isinstance(modem.online, bool)


@PROPERTY_SETTINGS
@given(st.text(max_size=2_000))
def test_remote_text_is_bounded_and_single_line(value: str) -> None:
    cleaned = remote_text(value)

    assert len(cleaned) <= MAX_REMOTE_PROPERTY_CHARS
    assert "\n" not in cleaned and "\r" not in cleaned and "\x1b" not in cleaned


@PROPERTY_SETTINGS
@given(st.text(max_size=200))
def test_any_accepted_dial_string_is_keypad_only(raw: str) -> None:
    try:
        number = normalize_dial_number(raw)
    except InvalidArgumentsError:
        return
    assert 1 <= len(number.lstrip("+")) <= MAX_DIAL_DIGITS
    assert set(number.lstrip("+")) <= set("0123456789")
