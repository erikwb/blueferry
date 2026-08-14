"""ANCS orchestration tests with no characteristic or bus access."""
from __future__ import annotations

import struct

from blueferry.ancs import client as client_module
from blueferry.ancs.client import AncsClient
from blueferry.ancs.constants import (
    CONTROL_POINT_CHAR,
    DATA_SOURCE_CHAR,
    MESSAGES_APP_ID,
    NOTIFICATION_SOURCE_CHAR,
    CommandID,
    EventFlag,
    EventID,
)
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


def _complete_authorization_probe(client: AncsClient) -> None:
    request = client._active_request
    assert request is not None
    assert request.authorization_probe is True
    response = (
        bytes([CommandID.GetAppAttributes])
        + MESSAGES_APP_ID.encode()
        + b"\0"
        + _attribute(0, "Messages")
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
    def __init__(self, managed=None) -> None:
        self.matches = []
        self.managed = managed or {}
        self.sweeps = 0

    def connect_to_signal(self, _name, _callback):
        match = _Match()
        self.matches.append(match)
        return match

    def GetManagedObjects(self, **_kwargs):
        self.sweeps += 1
        return self.managed


class _Bus:
    def __init__(self, manager) -> None:
        self.manager = manager
        self.owner_callback = None
        self.owner_match = None

    def add_signal_receiver(self, callback, **kwargs):
        assert kwargs["dbus_interface"] == "org.freedesktop.DBus"
        assert kwargs["signal_name"] == "NameOwnerChanged"
        assert kwargs["bus_name"] == "org.freedesktop.DBus"
        assert kwargs["arg0"] == "org.bluez"
        self.owner_callback = callback
        self.owner_match = _Match()
        return self.owner_match

    def get_object(self, _name, path):
        assert path == "/"
        return self.manager


class _CharacteristicBus:
    def __init__(self, characteristics) -> None:
        self.characteristics = characteristics

    def add_signal_receiver(self, *_args, **_kwargs):
        return _Match()

    def get_object(self, _name, path):
        return self.characteristics[path]


def test_start_is_idempotent(monkeypatch) -> None:
    manager = _ObjectManager()
    bus = _Bus(manager)
    monkeypatch.setattr(client_module, "get_system_bus", lambda: bus)
    monkeypatch.setattr(client_module.dbus, "Interface", lambda value, _iface: value)
    client = AncsClient("/device", lambda _event: None)

    client.start()
    client.start()

    assert len(manager.matches) == 2
    client.stop()
    assert all(match.removed for match in manager.matches)
    assert bus.owner_match.removed is True


def test_bluez_restart_rebinds_manager_and_rescans_cached_ancs_objects(
    monkeypatch,
) -> None:
    paths = {
        "/device/service0023/char0024": {
            "org.bluez.GattCharacteristic1": {"UUID": CONTROL_POINT_CHAR},
        },
        "/device/service0023/char0027": {
            "org.bluez.GattCharacteristic1": {"UUID": NOTIFICATION_SOURCE_CHAR},
        },
        "/device/service0023/char002a": {
            "org.bluez.GattCharacteristic1": {"UUID": DATA_SOURCE_CHAR},
        },
    }
    old_manager = _ObjectManager()
    new_manager = _ObjectManager(paths)
    bus = _Bus(old_manager)
    monkeypatch.setattr(client_module, "get_system_bus", lambda: bus)
    monkeypatch.setattr(client_module.dbus, "Interface", lambda value, _iface: value)
    statuses = []
    client = AncsClient("/device", lambda _event: None, on_status=lambda: statuses.append(True))
    monkeypatch.setattr(client, "_try_subscribe", lambda: None)

    client.start()
    client._notify_started = True
    client._authorized = True
    characteristic_matches = [_Match(), _Match()]
    client._characteristic_signal_matches = characteristic_matches

    assert bus.owner_callback is not None
    bus.owner_callback("org.bluez", ":1.10", "")

    assert client.connected is False
    assert client._ns_path is None
    assert client._ds_path is None
    assert client._cp_path is None
    assert all(match.removed for match in old_manager.matches)
    assert all(match.removed for match in characteristic_matches)
    assert statuses == [True]

    bus.manager = new_manager
    bus.owner_callback("org.bluez", "", ":1.11")

    assert new_manager.sweeps == 1
    assert len(new_manager.matches) == 2
    assert client._ns_path == "/device/service0023/char0027"
    assert client._ds_path == "/device/service0023/char002a"
    assert client._cp_path == "/device/service0023/char0024"


def test_bluez_restart_retries_when_object_manager_is_not_ready(monkeypatch) -> None:
    scheduled = []

    class _UnavailableManager(_ObjectManager):
        def GetManagedObjects(self, **_kwargs):
            raise RuntimeError("ObjectManager is not ready")

    old_manager = _ObjectManager()
    bus = _Bus(old_manager)
    monkeypatch.setattr(client_module, "get_system_bus", lambda: bus)
    monkeypatch.setattr(client_module.dbus, "Interface", lambda value, _iface: value)
    client = AncsClient(
        "/device",
        lambda _event: None,
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 17,
    )
    client.start()

    bus.manager = _UnavailableManager()
    assert bus.owner_callback is not None
    bus.owner_callback("org.bluez", ":1.10", ":1.11")

    assert scheduled[0][0] == client_module.MANAGER_RETRY_SECONDS
    assert client._manager_retry_id == 17

    ready_manager = _ObjectManager()
    bus.manager = ready_manager
    assert scheduled[0][1]() is False

    assert client._manager_retry_id is None
    assert ready_manager.sweeps == 1
    assert len(ready_manager.matches) == 2


def test_nested_owner_change_cannot_restore_the_losing_owner_objects(monkeypatch) -> None:
    stale_paths = {
        "/device/service-old/char-old": {
            "org.bluez.GattCharacteristic1": {"UUID": CONTROL_POINT_CHAR},
        },
    }
    current_paths = {
        "/device/service-new/char-new": {
            "org.bluez.GattCharacteristic1": {"UUID": CONTROL_POINT_CHAR},
        },
    }
    current_manager = _ObjectManager(current_paths)

    class _ReentrantManager(_ObjectManager):
        def GetManagedObjects(self, **_kwargs):
            bus.manager = current_manager
            assert bus.owner_callback is not None
            bus.owner_callback("org.bluez", ":1.10", ":1.11")
            return stale_paths

    initial_manager = _ObjectManager()
    bus = _Bus(initial_manager)
    monkeypatch.setattr(client_module, "get_system_bus", lambda: bus)
    monkeypatch.setattr(client_module.dbus, "Interface", lambda value, _iface: value)
    client = AncsClient("/device", lambda _event: None)
    monkeypatch.setattr(client, "_try_subscribe", lambda: None)
    client.start()

    bus.manager = _ReentrantManager()
    assert bus.owner_callback is not None
    bus.owner_callback("org.bluez", "", ":1.10")

    assert current_manager.sweeps == 1
    assert client._cp_path == "/device/service-new/char-new"
    assert client._cp_path != "/device/service-old/char-old"
    assert len(client._manager_signal_matches) == 2


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


def test_start_notify_failure_retries_without_rediscovery(monkeypatch) -> None:
    scheduled = []
    writes = []

    class _Characteristic:
        def __init__(self, *, fail_once: bool = False) -> None:
            self.fail_once = fail_once
            self.start_calls = 0

        def StartNotify(self, **_kwargs) -> None:
            self.start_calls += 1
            if self.fail_once:
                self.fail_once = False
                raise client_module.dbus.exceptions.DBusException(
                    "not ready",
                    name="org.bluez.Error.Failed",
                )

        def StopNotify(self, **_kwargs) -> None:
            pass

        def WriteValue(self, value, _options, **_kwargs) -> None:
            writes.append(bytes(value))

    ns = _Characteristic(fail_once=True)
    ds = _Characteristic()
    cp = _Characteristic()
    bus = _CharacteristicBus(
        {"/device/ns": ns, "/device/ds": ds, "/device/cp": cp}
    )
    monkeypatch.setattr(client_module, "get_system_bus", lambda: bus)
    monkeypatch.setattr(client_module.dbus, "Interface", lambda value, _iface: value)
    monkeypatch.setattr(
        client_module.GLib,
        "timeout_add_seconds",
        lambda _delay, _callback: 9,
    )
    monkeypatch.setattr(client_module.GLib, "source_remove", lambda _timer: None)
    client = AncsClient(
        "/device",
        lambda _event: None,
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
    )
    client._started = True
    client._ns_path = "/device/ns"
    client._ds_path = "/device/ds"
    client._cp_path = "/device/cp"

    client._try_subscribe()

    assert client.subscribed is False
    assert client.authorized is False
    assert client.connected is False
    assert len(scheduled) == 1
    assert scheduled[0][0] == client_module.SUBSCRIBE_RETRY_SECONDS

    scheduled[0][1]()

    assert client.subscribed is True
    assert client.authorized is False
    assert client.connected is False
    assert len(writes) == 1
    _complete_authorization_probe(client)
    assert client.subscribed is True
    assert client.authorized is True
    assert client.connected is True
    assert ns.start_calls == 2
    assert ds.start_calls == 1


def test_owner_change_during_start_notify_preserves_new_subscription(
    monkeypatch,
) -> None:
    outer_matches = []
    replacement_matches = [_Match(), _Match()]

    class _Characteristic:
        def __init__(self, *, changes_owner=False) -> None:
            self.changes_owner = changes_owner
            self.start_calls = 0

        def StartNotify(self, **_kwargs) -> None:
            self.start_calls += 1
            if self.changes_owner:
                client._bluez_owner_generation += 1
                client._characteristic_signal_matches = replacement_matches
                client._notify_started = True

    ns = _Characteristic(changes_owner=True)
    ds = _Characteristic()

    class _SubscribeBus:
        @staticmethod
        def add_signal_receiver(*_args, **_kwargs):
            match = _Match()
            outer_matches.append(match)
            return match

        @staticmethod
        def get_object(_name, path):
            return {"/device/ns": ns, "/device/ds": ds}[path]

    monkeypatch.setattr(client_module, "get_system_bus", lambda: _SubscribeBus())
    monkeypatch.setattr(client_module.dbus, "Interface", lambda value, _iface: value)
    client = AncsClient("/device", lambda _event: None)
    client._started = True
    client._ns_path = "/device/ns"
    client._ds_path = "/device/ds"
    client._cp_path = "/device/cp"

    client._try_subscribe()

    assert ns.start_calls == 1
    assert ds.start_calls == 0
    assert all(match.removed for match in outer_matches)
    assert client._characteristic_signal_matches == replacement_matches
    assert client.subscribed is True


def test_owner_change_during_control_point_write_preserves_new_request(
    monkeypatch,
) -> None:
    timeout_calls = []
    replacement_request = object()

    class _ControlPoint:
        @staticmethod
        def WriteValue(_value, _options, **_kwargs) -> None:
            client._bluez_owner_generation += 1
            client._request_queue.clear()
            client._active_request = replacement_request
            client._request_timeout_id = 91

    bus = _CharacteristicBus({"/device/cp": _ControlPoint()})
    monkeypatch.setattr(client_module, "get_system_bus", lambda: bus)
    monkeypatch.setattr(client_module.dbus, "Interface", lambda value, _iface: value)
    monkeypatch.setattr(
        client_module.GLib,
        "timeout_add_seconds",
        lambda *args: timeout_calls.append(args) or 92,
    )
    client = AncsClient("/device", lambda _event: None)
    client._started = True
    client._notify_started = True
    client._cp_path = "/device/cp"

    client._queue_authorization_probe()

    assert client._active_request is replacement_request
    assert client._request_timeout_id == 91
    assert timeout_calls == []


def test_control_point_failure_keeps_ancs_unready_and_retries(
    monkeypatch, caplog,
) -> None:
    scheduled = []

    class _ControlPoint:
        def __init__(self) -> None:
            self.fail = True

        def WriteValue(self, _value, _options, **_kwargs) -> None:
            if self.fail:
                self.fail = False
                raise client_module.dbus.exceptions.DBusException(
                    "Insufficient authorization",
                    name="org.bluez.Error.Failed",
                )

    cp = _ControlPoint()
    bus = _CharacteristicBus({"/device/cp": cp})
    monkeypatch.setattr(client_module, "get_system_bus", lambda: bus)
    monkeypatch.setattr(client_module.dbus, "Interface", lambda value, _iface: value)
    monkeypatch.setattr(
        client_module.GLib,
        "timeout_add_seconds",
        lambda _delay, _callback: 9,
    )
    monkeypatch.setattr(client_module.GLib, "source_remove", lambda _timer: None)
    client = AncsClient(
        "/device",
        lambda _event: None,
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
    )
    client._started = True
    client._notify_started = True
    client._cp_path = "/device/cp"

    client._queue_authorization_probe()

    assert client.subscribed is True
    assert client.authorized is False
    assert client.connected is False
    assert scheduled[0][0] == client_module.AUTHORIZATION_RETRY_SECONDS
    assert "org.bluez.Error.Failed: Insufficient authorization" in caplog.text

    scheduled[0][1]()
    assert client.connected is False
    _complete_authorization_probe(client)
    assert client.authorized is True
    assert client.connected is True
