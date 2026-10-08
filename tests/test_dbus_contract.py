"""Canonical introspection XML must match the exported dbus-python service."""
from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree

from blueferry.dbus_service import MessagesService
from blueferry.protocol import (
    CALLS_IFACE,
    EVENTS_IFACE,
    MEDIA_IFACE,
    MESSAGES_IFACE,
    OBJECT_PATH,
    PRESENCE_IFACE,
)

CONTRACT = Path(__file__).resolve().parents[1] / "data/io.weirdware.BlueFerry.xml"


def _signature(member, direction: str) -> str:
    return "".join(
        argument.attrib["type"]
        for argument in member.findall("arg")
        if argument.attrib.get("direction", "out") == direction
    )


def _check_methods(node, interface_name: str) -> None:
    interface = node.find(f"interface[@name='{interface_name}']")
    assert interface is not None, interface_name
    xml_methods = {method.attrib["name"]: method for method in interface.findall("method")}
    exported_methods = {
        name: member
        for name, member in vars(MessagesService).items()
        if getattr(member, "_dbus_interface", None) == interface_name
        and getattr(member, "_dbus_is_method", False)
    }
    assert xml_methods.keys() == exported_methods.keys()
    for name, member in exported_methods.items():
        assert _signature(xml_methods[name], "in") == member._dbus_in_signature
        assert _signature(xml_methods[name], "out") == member._dbus_out_signature


def test_contract_matches_exported_methods_and_signals() -> None:
    node = ElementTree.parse(CONTRACT).getroot()
    assert node.attrib["name"] == OBJECT_PATH
    assert {
        interface.attrib["name"] for interface in node.findall("interface")
    } == {MESSAGES_IFACE, EVENTS_IFACE, PRESENCE_IFACE, CALLS_IFACE, MEDIA_IFACE}

    _check_methods(node, MESSAGES_IFACE)
    _check_methods(node, PRESENCE_IFACE)
    _check_methods(node, MEDIA_IFACE)
    _check_methods(node, CALLS_IFACE)
    exported_interfaces = {
        getattr(member, "_dbus_interface", None)
        for member in vars(MessagesService).values()
    } - {None}
    assert exported_interfaces == {
        MESSAGES_IFACE, EVENTS_IFACE, PRESENCE_IFACE, CALLS_IFACE, MEDIA_IFACE,
    }

    events = node.find(f"interface[@name='{EVENTS_IFACE}']")
    assert events is not None
    xml_signals = {signal.attrib["name"]: signal for signal in events.findall("signal")}
    exported_signals = {
        name: member
        for name, member in vars(MessagesService).items()
        if getattr(member, "_dbus_interface", None) == EVENTS_IFACE
        and getattr(member, "_dbus_is_signal", False)
    }
    assert xml_signals.keys() == exported_signals.keys()
    for name, member in exported_signals.items():
        assert _signature(xml_signals[name], "out") == member._dbus_signature


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
