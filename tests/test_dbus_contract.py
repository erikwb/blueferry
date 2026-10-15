"""Canonical introspection XML must match the exported dbus-python service."""
from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree

from blueferry.dbus_service import MessagesService
from blueferry.protocol import (
    CALL_HISTORY_IFACE,
    CALLS_IFACE,
    EVENTS_IFACE,
    MEDIA_IFACE,
    MESSAGES_IFACE,
    OBJECT_PATH,
    PRESENCE_IFACE,
    TETHER_IFACE,
)

CONTRACT = Path(__file__).resolve().parents[1] / "data/io.weirdware.BlueFerry.xml"


def _signature(member, direction: str) -> str:
    return "".join(
        argument.attrib["type"]
        for argument in member.findall("arg")
        if argument.attrib.get("direction", "out") == direction
    )


def _exported(interface: str, kind: str) -> dict:
    return {
        name: member
        for name, member in vars(MessagesService).items()
        if getattr(member, "_dbus_interface", None) == interface
        and getattr(member, kind, False)
    }


def test_contract_matches_exported_methods_and_signals() -> None:
    node = ElementTree.parse(CONTRACT).getroot()
    assert node.attrib["name"] == OBJECT_PATH
    interfaces = (
        MESSAGES_IFACE, EVENTS_IFACE, PRESENCE_IFACE, CALL_HISTORY_IFACE,
        CALLS_IFACE, MEDIA_IFACE, TETHER_IFACE,
    )
    assert {
        interface.attrib["name"] for interface in node.findall("interface")
    } == set(interfaces)

    exported_interfaces = {
        getattr(member, "_dbus_interface", None)
        for member in vars(MessagesService).values()
    } - {None}
    assert exported_interfaces == set(interfaces)
    for interface_name in interfaces:
        interface = node.find(f"interface[@name='{interface_name}']")
        assert interface is not None, interface_name
        xml_methods = {method.attrib["name"]: method for method in interface.findall("method")}
        exported_methods = _exported(interface_name, "_dbus_is_method")
        assert xml_methods.keys() == exported_methods.keys()
        for name, member in exported_methods.items():
            assert _signature(xml_methods[name], "in") == member._dbus_in_signature
            assert _signature(xml_methods[name], "out") == member._dbus_out_signature
        xml_signals = {signal.attrib["name"]: signal for signal in interface.findall("signal")}
        exported_signals = _exported(interface_name, "_dbus_is_signal")
        assert xml_signals.keys() == exported_signals.keys()
        for name, member in exported_signals.items():
            assert _signature(xml_signals[name], "out") == member._dbus_signature


def test_tether_interface_is_small_and_its_signal_is_content_free() -> None:
    assert set(_exported(TETHER_IFACE, "_dbus_is_method")) == {
        "Connect", "Disconnect", "GetState", "SetTethering",
    }
    signals = _exported(TETHER_IFACE, "_dbus_is_signal")
    assert set(signals) == {"TetherChanged"}
    assert signals["TetherChanged"]._dbus_signature == ""
    # Tethering never widens the messaging generation's Events1 contract.
    assert set(_exported(EVENTS_IFACE, "_dbus_is_signal")) == {
        "HistoryChanged", "StatusChanged", "OpenMessageRequested",
        "NowPlayingChanged", "CallsChanged", "CallHistoryChanged",
    }


def test_every_documented_error_has_the_stable_namespace() -> None:
    root = ElementTree.parse(CONTRACT).getroot()
    annotations = root.findall(".//annotation[@name='io.weirdware.BlueFerry.Errors']")

    errors = {
        value
        for annotation in annotations
        for value in annotation.attrib["value"].split(",")
    }

    assert errors == {
        "AuthorizationRequired",
        "CallFailed",
        "CallHistorySyncFailed",
        "CallsDisabled",
        "CallsUnavailable",
        "ConfirmationRequired",
        "ContactSyncFailed",
        "InvalidArgs",
        "MediaCommandFailed",
        "NotFound",
        "NotReady",
        "QueryFailed",
        "RateLimited",
        "ResponseTooLarge",
        "SendFailed",
        "SendOutcomeUnknown",
    }


def test_events_signals_carry_no_media_content() -> None:
    """NowPlayingChanged is an argument-free invalidation, like StatusChanged."""
    root = ElementTree.parse(CONTRACT).getroot()
    signal = root.find(
        f"interface[@name='{EVENTS_IFACE}']/signal[@name='NowPlayingChanged']"
    )
    assert signal is not None
    assert signal.findall("arg") == []
    assert MessagesService.NowPlayingChanged._dbus_signature == ""


def test_calls_changed_signal_is_content_free() -> None:
    root = ElementTree.parse(CONTRACT).getroot()
    events = root.find(f"interface[@name='{EVENTS_IFACE}']")
    assert events is not None
    signal = events.find("signal[@name='CallsChanged']")

    assert signal is not None
    assert signal.findall("arg") == []
    assert MessagesService.CallsChanged._dbus_signature == ""


def test_call_errors_map_to_their_documented_names() -> None:
    from blueferry.errors import CallsDisabledError, CallsUnavailableError, OperationFailedError

    assert MessagesService._dbus_error(CallsDisabledError("x")).get_dbus_name() == (
        "io.weirdware.BlueFerry.Error.CallsDisabled"
    )
    assert MessagesService._dbus_error(CallsUnavailableError("x")).get_dbus_name() == (
        "io.weirdware.BlueFerry.Error.CallsUnavailable"
    )
    failed = MessagesService._dbus_error(OperationFailedError("Call", RuntimeError("+4179 secret")))
    assert failed.get_dbus_name() == "io.weirdware.BlueFerry.Error.CallFailed"
    assert "+4179" not in failed.get_dbus_message()
