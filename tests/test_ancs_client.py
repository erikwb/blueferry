"""ANCS orchestration tests with no characteristic or bus access."""
from __future__ import annotations

import struct

from blueferry.ancs import client as client_module
from blueferry.ancs.client import AncsClient
from blueferry.ancs.constants import CommandID, EventFlag, EventID
from blueferry.ancs.parsers import Notification


def _notification(uid: int, *, flags: int = 0) -> bytes:
    return struct.pack("<BBBBI", EventID.NotificationAdded, flags, 4, 1, uid)


def _attribute(attribute_id: int, value: str) -> bytes:
    encoded = value.encode()
    return bytes([attribute_id]) + struct.pack("<H", len(encoded)) + encoded


def _complete_app_probe(client: AncsClient, uid: int, app_id: str) -> None:
    request = client._request_queue.popleft()
    client._active_request = request
    response = (
        bytes([CommandID.GetNotificationAttributes])
        + struct.pack("<I", uid)
        + _attribute(0, app_id)
    )
    client._on_ds_changed(
        "org.bluez.GattCharacteristic1", {"Value": response}, []
    )


def _complete_full_response(
    client: AncsClient, uid: int, app_id: str,
) -> None:
    request = client._request_queue.popleft()
    client._active_request = request
    response = (
        bytes([CommandID.GetNotificationAttributes])
        + struct.pack("<I", uid)
        + _attribute(0, app_id)
        + _attribute(1, "Alice")
        + _attribute(2, "To you & Bob")
        + _attribute(3, "hello")
    )
    client._on_ds_changed(
        "org.bluez.GattCharacteristic1", {"Value": response}, []
    )


def test_duplicate_notification_uid_is_coalesced_before_control_point_exists() -> None:
    client = AncsClient("/device", lambda _event: None)
    changed = {"Value": _notification(42)}

    client._on_ns_changed("org.bluez.GattCharacteristic1", changed, [])
    client._on_ns_changed("org.bluez.GattCharacteristic1", changed, [])

    assert len(client._request_queue) == 1


def test_preexisting_notification_never_enters_request_backlog() -> None:
    client = AncsClient("/device", lambda _event: None)
    changed = {"Value": _notification(42, flags=EventFlag.PreExisting)}

    client._on_ns_changed("org.bluez.GattCharacteristic1", changed, [])

    assert len(client._request_queue) == 0


def test_default_policy_discards_non_message_after_identifier_probe() -> None:
    emitted = []
    client = AncsClient("/device", emitted.append)
    notification = Notification.parse(_notification(42))
    client._request_attrs(notification)

    _complete_app_probe(client, 42, "com.example.Private")

    assert len(client._request_queue) == 0
    assert emitted == []


def test_messages_identifier_queues_full_attributes_for_grouping() -> None:
    client = AncsClient("/device", lambda _event: None)
    notification = Notification.parse(_notification(42))
    client._request_attrs(notification)

    _complete_app_probe(client, 42, "com.apple.MobileSMS")

    assert len(client._request_queue) == 1
    request = client._request_queue.popleft()
    assert request.app_probe is False
    assert request.expected_app_id == "com.apple.MobileSMS"
    assert b"com.apple.MobileSMS" not in request.packet


def test_unused_action_labels_are_not_requested() -> None:
    client = AncsClient("/device", lambda _event: None)
    flags = EventFlag.PositiveAction | EventFlag.NegativeAction
    notification = Notification.parse(_notification(42, flags=flags))
    client._request_attrs(notification)

    _complete_app_probe(client, 42, "com.apple.MobileSMS")

    request = client._request_queue.popleft()
    assert request.assembler.attribute_ids == (0, 1, 2, 3)


def test_messages_full_response_emits_without_app_name_lookup() -> None:
    emitted = []
    client = AncsClient("/device", emitted.append)
    notification = Notification.parse(_notification(42))
    client._request_attrs(notification)
    _complete_app_probe(client, 42, "com.apple.MobileSMS")

    _complete_full_response(client, 42, "com.apple.MobileSMS")

    assert len(client._request_queue) == 0
    assert len(emitted) == 1
    assert emitted[0].app_id == "com.apple.MobileSMS"
    assert emitted[0].body == "hello"


def test_all_policy_reads_future_well_formed_system_notifications() -> None:
    client = AncsClient(
        "/device",
        lambda _event: None,
        include_non_message_notifications=lambda: True,
    )
    notification = Notification.parse(_notification(42))
    client._request_attrs(notification)

    _complete_app_probe(client, 42, "com.example.Private")

    assert len(client._request_queue) == 1


def test_malformed_app_identifier_is_discarded_even_under_all_policy() -> None:
    client = AncsClient(
        "/device",
        lambda _event: None,
        include_non_message_notifications=lambda: True,
    )
    notification = Notification.parse(_notification(42))
    client._request_attrs(notification)

    _complete_app_probe(client, 42, "bad\nidentifier")

    assert len(client._request_queue) == 0


class _Match:
    def __init__(self) -> None:
        self.removed = False

    def remove(self) -> None:
        self.removed = True


class _ObjectManager:
    def __init__(self) -> None:
        self.matches = []

    def connect_to_signal(self, _name, _callback):
        match = _Match()
        self.matches.append(match)
        return match

    def GetManagedObjects(self, **_kwargs):
        return {}


class _Bus:
    def __init__(self, manager) -> None:
        self.manager = manager

    def get_object(self, _name, path):
        assert path == "/"
        return self.manager


def test_start_is_idempotent(monkeypatch) -> None:
    manager = _ObjectManager()
    monkeypatch.setattr(client_module, "get_system_bus", lambda: _Bus(manager))
    monkeypatch.setattr(client_module.dbus, "Interface", lambda value, _iface: value)
    client = AncsClient("/device", lambda _event: None)

    client.start()
    client.start()

    assert len(manager.matches) == 2
    client.stop()
    assert all(match.removed for match in manager.matches)


def test_characteristic_removal_discards_all_characteristic_receivers(
    monkeypatch,
) -> None:
    stopped = []

    class _Characteristic:
        def StopNotify(self, **_kwargs) -> None:
            stopped.append(True)

    class _CharacteristicBus:
        def get_object(self, _name, _path):
            return _Characteristic()

    monkeypatch.setattr(
        client_module, "get_system_bus", lambda: _CharacteristicBus()
    )
    monkeypatch.setattr(client_module.dbus, "Interface", lambda value, _iface: value)
    client = AncsClient("/device", lambda _event: None)
    client._ns_path = "/device/service/ns"
    client._ds_path = "/device/service/ds"
    client._cp_path = "/device/service/cp"
    client._notify_started = True
    matches = [_Match(), _Match()]
    client._characteristic_signal_matches = matches

    client._on_iface_removed(client._ns_path, [])

    assert client._notify_started is False
    assert client._characteristic_signal_matches == []
    assert all(match.removed for match in matches)
    assert stopped == [True]
