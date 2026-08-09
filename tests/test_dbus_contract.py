"""Canonical introspection XML must match the exported dbus-python service."""
from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree

from blueferry.dbus_service import MessagesService
from blueferry.protocol import EVENTS_IFACE, MESSAGES_IFACE, OBJECT_PATH

CONTRACT = Path(__file__).resolve().parents[1] / "data/io.weirdware.BlueFerry.xml"


def _signature(member, direction: str) -> str:
    return "".join(
        argument.attrib["type"]
        for argument in member.findall("arg")
        if argument.attrib.get("direction", "out") == direction
    )


def test_contract_matches_exported_methods_and_signals() -> None:
    node = ElementTree.parse(CONTRACT).getroot()
    assert node.attrib["name"] == OBJECT_PATH

    messages = node.find(f"interface[@name='{MESSAGES_IFACE}']")
    assert messages is not None
    xml_methods = {method.attrib["name"]: method for method in messages.findall("method")}
    exported_methods = {
        name: member
        for name, member in vars(MessagesService).items()
        if getattr(member, "_dbus_interface", None) == MESSAGES_IFACE
        and getattr(member, "_dbus_is_method", False)
    }
    assert xml_methods.keys() == exported_methods.keys()
    for name, member in exported_methods.items():
        assert _signature(xml_methods[name], "in") == member._dbus_in_signature
        assert _signature(xml_methods[name], "out") == member._dbus_out_signature

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
        "ConfirmationRequired",
        "ContactSyncFailed",
        "InvalidArgs",
        "NotFound",
        "NotReady",
        "QueryFailed",
        "RateLimited",
        "ResponseTooLarge",
        "SendFailed",
    }
