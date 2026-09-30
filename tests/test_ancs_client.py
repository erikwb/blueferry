"""ANCS orchestration tests with no characteristic or bus access."""
from __future__ import annotations

import struct

import pytest

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


def test_health_probe_requires_a_new_response_and_does_not_read_messages(monkeypatch):
    client = AncsClient("/device", lambda _event: None)
    client._bearer_connected = True
    client._bearer_ready = True
    client._notify_started = True
    client._authorized = True
    client.health_proof = 10.0
    monkeypatch.setattr(client, "_pump_requests", lambda: None)
    monkeypatch.setattr(client_module.time, "monotonic", lambda: 100.0)

    client.probe_health()
    assert client.health_proof == 10.0
    client._active_request = client._request_queue.popleft()
    assert client._active_request.notification is None
    assert client._active_request.assembler.command == CommandID.GetAppAttributes
    client.probe_health()
    assert not client._request_queue
    _complete_authorization_probe(client)
    assert client.health_proof == 100.0


def test_explicit_permission_failure_blocks_power_recovery_until_authorized():
    import dbus

    client = AncsClient("/device", lambda _event: None)
    client._observe_permission_error(dbus.exceptions.DBusException(
        "permission denied", name="org.bluez.Error.NotAuthorized",
    ))
    assert client.permission_denied
    client._mark_authorized()
    assert not client.permission_denied


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


def test_app_filter_discards_before_notification_content_is_requested() -> None:
    considered = []
    client = AncsClient(
        "/device",
        lambda _event: None,
        include_non_message_notifications=lambda: True,
        include_app_notification=lambda app_id: (
            considered.append(app_id) or False
        ),
    )
    notification = Notification.parse(_notification(42))
    client._request_attrs(notification)

    _complete_app_probe(client, 42, "com.example.Blocked")

    assert considered == ["com.example.Blocked"]
    assert len(client._request_queue) == 0


def test_app_filter_allows_matching_notification_content_request() -> None:
    client = AncsClient(
        "/device",
        lambda _event: None,
        include_non_message_notifications=lambda: True,
        include_app_notification=lambda app_id: app_id == "com.example.Allowed",
    )
    notification = Notification.parse(_notification(42))
    client._request_attrs(notification)

    _complete_app_probe(client, 42, "com.example.Allowed")

    assert len(client._request_queue) == 1
    assert client._request_queue.popleft().expected_app_id == "com.example.Allowed"


def test_messages_bypasses_non_message_app_filter_for_grouping() -> None:
    client = AncsClient(
        "/device",
        lambda _event: None,
        include_non_message_notifications=lambda: True,
        include_app_notification=lambda _app_id: False,
    )
    notification = Notification.parse(_notification(42))
    client._request_attrs(notification)

    _complete_app_probe(client, 42, "com.apple.MobileSMS")

    assert len(client._request_queue) == 1
    assert client._request_queue.popleft().expected_app_id == (
        "com.apple.MobileSMS"
    )


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

    def get_object(self, _name, path):
        assert path == "/"
        return self.manager


class _CharacteristicBus:
    def __init__(self, characteristics) -> None:
        self.characteristics = characteristics

    def add_signal_receiver(self, *_args, **_kwargs):
        return _Match()

    def get_object(self, _name, path, **_kwargs):
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
    client = AncsClient(
        "/device",
        lambda _event: None,
        on_status=lambda: statuses.append(True),
    )
    monkeypatch.setattr(client, "_try_subscribe", lambda: None)

    client.start()
    client._bearer_connected = True
    client._bearer_ready = True
    client._notify_started = True
    client._authorized = True
    characteristic_matches = [_Match(), _Match()]
    client._characteristic_signal_matches = characteristic_matches

    client.observe_bluez_owner(":1.10", "")

    assert client.connected is False
    assert client._ns_path is None
    assert client._ds_path is None
    assert client._cp_path is None
    assert all(match.removed for match in old_manager.matches)
    assert all(match.removed for match in characteristic_matches)
    assert statuses == [True]

    bus.manager = new_manager
    client.observe_bluez_owner("", ":1.11")

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
        cancel=lambda _timer: None,
    )
    client.start()

    bus.manager = _UnavailableManager()
    client.observe_bluez_owner(":1.10", ":1.11")

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
            client.observe_bluez_owner(":1.10", ":1.11")
            return stale_paths

    initial_manager = _ObjectManager()
    bus = _Bus(initial_manager)
    monkeypatch.setattr(client_module, "get_system_bus", lambda: bus)
    monkeypatch.setattr(client_module.dbus, "Interface", lambda value, _iface: value)
    client = AncsClient("/device", lambda _event: None)
    monkeypatch.setattr(client, "_try_subscribe", lambda: None)
    client.start()

    bus.manager = _ReentrantManager()
    client.observe_bluez_owner("", ":1.10")

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
    # The characteristic is already disappearing; issuing StopNotify here can
    # race BlueZ's pending CCC completion with ATT teardown.
    assert stopped == []


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

        def WriteValue(self, value, _options, **kwargs) -> None:
            writes.append(bytes(value))
            kwargs["reply_handler"]()

    ns = _Characteristic(fail_once=True)
    ds = _Characteristic()
    cp = _Characteristic()
    bus = _CharacteristicBus(
        {"/device/ns": ns, "/device/ds": ds, "/device/cp": cp}
    )
    monkeypatch.setattr(client_module, "get_system_bus", lambda: bus)
    monkeypatch.setattr(client_module.dbus, "Interface", lambda value, _iface: value)
    client = AncsClient(
        "/device",
        lambda _event: None,
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
        cancel=lambda _timer: None,
    )
    client._started = True
    client._bearer_connected = True
    client._bearer_ready = True
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


def test_initial_subscription_waits_for_a_settled_le_bearer(monkeypatch) -> None:
    calls = []

    class _Characteristic:
        def __init__(self, name: str) -> None:
            self.name = name

        def StartNotify(self, **_kwargs) -> None:
            calls.append(("start", self.name))

        def WriteValue(self, _value, _options, **kwargs) -> None:
            calls.append(("write", self.name))
            kwargs["reply_handler"]()

    bus = _CharacteristicBus({
        "/device/ns": _Characteristic("ns"),
        "/device/ds": _Characteristic("ds"),
        "/device/cp": _Characteristic("cp"),
    })
    monkeypatch.setattr(client_module, "get_system_bus", lambda: bus)
    monkeypatch.setattr(client_module.dbus, "Interface", lambda value, _iface: value)
    scheduled = []
    client = AncsClient(
        "/device",
        lambda _event: None,
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 9,
        cancel=lambda _timer: None,
    )
    client._started = True
    client._bearer_connected = False
    client._bearer_ready = False
    client._ns_path = "/device/ns"
    client._ds_path = "/device/ds"
    client._cp_path = "/device/cp"

    client._try_subscribe()

    assert calls == []
    assert client.subscribed is False

    client.observe_bearer_state(True)
    assert client._bearer_settle_id == 9
    assert [delay for delay, _callback in scheduled] == [
        client_module.BEARER_SETTLE_SECONDS
    ]


def test_missing_att_transport_resets_a_connected_le_bearer(monkeypatch) -> None:
    scheduled = []
    resets = []

    class _Characteristic:
        @staticmethod
        def StartNotify(**_kwargs) -> None:
            raise client_module.dbus.exceptions.DBusException(
                "No ATT transport",
                name="org.bluez.Error.Failed",
            )

    bus = _CharacteristicBus({
        "/device/ns": _Characteristic(),
        "/device/ds": _Characteristic(),
        "/device/cp": _Characteristic(),
    })
    monkeypatch.setattr(client_module, "get_system_bus", lambda: bus)
    monkeypatch.setattr(client_module.dbus, "Interface", lambda value, _iface: value)
    client = AncsClient(
        "/device",
        lambda _event: None,
        on_transport_failure=lambda: resets.append(True),
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
        cancel=lambda _timer: None,
    )
    client._started = True
    client._bearer_connected = True
    client._bearer_ready = True
    client._ns_path = "/device/ns"
    client._ds_path = "/device/ds"
    client._cp_path = "/device/cp"

    client._try_subscribe()

    assert client.subscribed is False
    assert client._transport_blocked is True
    assert scheduled[0][0] == client_module.TRANSPORT_RESET_SECONDS
    scheduled[0][1]()
    assert resets == [True]


def test_previously_authorized_subscription_waits_for_a_settled_bearer(
    monkeypatch,
) -> None:
    calls = []
    scheduled = []

    class _Characteristic:
        def __init__(self, name: str) -> None:
            self.name = name

        def StartNotify(self, **_kwargs) -> None:
            calls.append(("start", self.name))

        def WriteValue(self, _value, _options, **kwargs) -> None:
            calls.append(("write", self.name))
            kwargs["reply_handler"]()

    bus = _CharacteristicBus({
        "/device/ns": _Characteristic("ns"),
        "/device/ds": _Characteristic("ds"),
        "/device/cp": _Characteristic("cp"),
    })
    monkeypatch.setattr(client_module, "get_system_bus", lambda: bus)
    monkeypatch.setattr(client_module.dbus, "Interface", lambda value, _iface: value)
    client = AncsClient(
        "/device",
        lambda _event: None,
        previously_authorized=True,
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
        cancel=lambda _timer: None,
    )
    client._started = True
    client._bearer_connected = False
    client._bearer_ready = False
    client._ns_path = "/device/ns"
    client._ds_path = "/device/ds"
    client._cp_path = "/device/cp"

    client._try_subscribe()

    assert calls == []
    assert scheduled == []

    client.observe_bearer_state(True)

    assert scheduled[0][0] == client_module.BEARER_SETTLE_SECONDS
    scheduled[0][1]()
    assert calls == [
        ("start", "ns"),
        ("start", "ds"),
        ("write", "cp"),
    ]


def test_partial_start_notify_failure_reuses_live_subscription(monkeypatch) -> None:
    scheduled = []

    class _Characteristic:
        def __init__(self, *, fail_once: bool = False) -> None:
            self.fail_once = fail_once
            self.notifying = False
            self.start_calls = 0
            self.stop_calls = 0

        def Get(self, _interface, name, **_kwargs):
            assert name == "Notifying"
            return self.notifying

        def StartNotify(self, **_kwargs) -> None:
            self.start_calls += 1
            if self.fail_once:
                self.fail_once = False
                raise client_module.dbus.exceptions.DBusException(
                    "not ready",
                    name="org.bluez.Error.Failed",
                )
            self.notifying = True

        def StopNotify(self, **_kwargs) -> None:
            self.stop_calls += 1

        @staticmethod
        def WriteValue(_value, _options, **kwargs) -> None:
            kwargs["reply_handler"]()

    ns = _Characteristic()
    ds = _Characteristic(fail_once=True)
    cp = _Characteristic()
    bus = _CharacteristicBus(
        {"/device/ns": ns, "/device/ds": ds, "/device/cp": cp}
    )
    monkeypatch.setattr(client_module, "get_system_bus", lambda: bus)
    monkeypatch.setattr(client_module.dbus, "Interface", lambda value, _iface: value)
    client = AncsClient(
        "/device",
        lambda _event: None,
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
        cancel=lambda _timer: None,
    )
    client._started = True
    client._bearer_connected = True
    client._bearer_ready = True
    client._ns_path = "/device/ns"
    client._ds_path = "/device/ds"
    client._cp_path = "/device/cp"

    client._try_subscribe()

    assert ns.start_calls == 1
    assert ns.stop_calls == 0
    assert ds.start_calls == 1
    assert client.subscribed is False

    scheduled[0][1]()

    assert ns.start_calls == 1
    assert ns.stop_calls == 0
    assert ds.start_calls == 2
    assert client.subscribed is True


@pytest.mark.parametrize("stale_registration", [False, True])
def test_le_reconnect_refreshes_registrations_only_after_a_silent_probe(
    monkeypatch, stale_registration,
) -> None:
    calls = []
    statuses = []
    scheduled = []

    class _Characteristic:
        def __init__(self, name: str) -> None:
            self.name = name

        @staticmethod
        def Get(_iface, name, **_kwargs):
            assert name == "Notifying"
            return True

        def StartNotify(self, **_kwargs) -> None:
            calls.append(("start", self.name))

        def StopNotify(self, **_kwargs) -> None:
            calls.append(("stop", self.name))

        def WriteValue(self, _value, _options, **kwargs) -> None:
            calls.append(("write", self.name))
            kwargs["reply_handler"]()

    ns = _Characteristic("ns")
    ds = _Characteristic("ds")
    cp = _Characteristic("cp")
    bus = _CharacteristicBus(
        {"/device/ns": ns, "/device/ds": ds, "/device/cp": cp}
    )
    monkeypatch.setattr(client_module, "get_system_bus", lambda: bus)
    monkeypatch.setattr(client_module.dbus, "Interface", lambda value, _iface: value)
    client = AncsClient(
        "/device",
        lambda _event: None,
        on_status=lambda: statuses.append(True),
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
        cancel=lambda _timer: None,
    )
    client._started = True
    client._bearer_connected = True
    client._bearer_ready = True
    client._ns_path = "/device/ns"
    client._ds_path = "/device/ds"
    client._cp_path = "/device/cp"
    client._notify_started = True
    client._authorized = True
    client._was_authorized = True
    client._owned_notify_paths = {"/device/ns", "/device/ds"}
    old_matches = [_Match(), _Match()]
    client._characteristic_signal_matches = old_matches

    client.observe_bearer_state(False)

    assert client.connected is False
    assert client.subscribed is False
    assert client.authorized is False
    assert all(match.removed for match in old_matches)
    assert calls == []
    assert statuses == [True]

    client.observe_bearer_state(True)

    assert calls == []
    assert scheduled[0][0] == client_module.BEARER_SETTLE_SECONDS
    scheduled[0][1]()

    # Owned CCC registrations stay in place. Rewriting them on an LE flap
    # SIGSEGVs bluetoothd 5.87. Control Point still has to prove the ATT
    # session can write before we report connected.
    assert calls == [("write", "cp")]
    assert client.subscribed is True
    assert client.authorized is False
    assert client.connected is False
    if stale_registration:
        client._request_timed_out()
        assert calls == [("write", "cp"), ("stop", "ns"), ("stop", "ds")]
        assert client._owned_notify_paths == set()
        assert not client.connected
        client.observe_bearer_state(False)
        client.observe_bearer_state(True)
        scheduled[-1][1]()
        # Even a stale Notifying=true flag must not suppress fresh ownership.
        assert calls[-3:] == [("start", "ns"), ("start", "ds"), ("write", "cp")]
    _complete_authorization_probe(client)
    assert client.authorized is True
    assert client.connected is True
    assert statuses == [True, True]


def test_le_reconnect_does_not_stop_notify_while_att_may_be_down(
    monkeypatch,
) -> None:
    scheduled = []
    calls = []

    class _Characteristic:
        def __init__(self, name: str) -> None:
            self.name = name

        @staticmethod
        def Get(_iface, name, **_kwargs):
            assert name == "Notifying"
            return True

        def StartNotify(self, **_kwargs) -> None:
            calls.append(("start", self.name))

        def StopNotify(self, **_kwargs) -> None:
            calls.append(("stop", self.name))

        @staticmethod
        def WriteValue(_value, _options, **kwargs) -> None:
            calls.append("write")
            kwargs["reply_handler"]()

    ns = _Characteristic("ns")
    ds = _Characteristic("ds")
    cp = _Characteristic("cp")
    bus = _CharacteristicBus(
        {"/device/ns": ns, "/device/ds": ds, "/device/cp": cp}
    )
    monkeypatch.setattr(client_module, "get_system_bus", lambda: bus)
    monkeypatch.setattr(client_module.dbus, "Interface", lambda value, _iface: value)
    client = AncsClient(
        "/device",
        lambda _event: None,
        previously_authorized=True,
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
        cancel=lambda _timer: None,
    )
    client._started = True
    client._bearer_connected = False
    client._ns_path = "/device/ns"
    client._ds_path = "/device/ds"
    client._cp_path = "/device/cp"
    client._owned_notify_paths = {"/device/ns", "/device/ds"}

    client.observe_bearer_state(True)
    scheduled.pop(0)[1]()

    assert calls == ["write"]
    assert client.subscribed is True
    assert client.authorized is False


def test_le_reconnect_starts_notify_only_when_bluez_dropped_ccc(
    monkeypatch,
) -> None:
    scheduled = []
    calls = []

    class _Characteristic:
        def __init__(self, name: str, *, notifying: bool) -> None:
            self.name = name
            self.notifying = notifying

        def Get(self, _iface, name, **_kwargs):
            assert name == "Notifying"
            return self.notifying

        def StartNotify(self, **_kwargs) -> None:
            calls.append(("start", self.name))
            self.notifying = True

        def StopNotify(self, **_kwargs) -> None:
            calls.append(("stop", self.name))

        @staticmethod
        def WriteValue(_value, _options, **kwargs) -> None:
            calls.append("write")
            kwargs["reply_handler"]()

    ns = _Characteristic("ns", notifying=False)
    ds = _Characteristic("ds", notifying=True)
    cp = _Characteristic("cp", notifying=False)
    bus = _CharacteristicBus(
        {"/device/ns": ns, "/device/ds": ds, "/device/cp": cp}
    )
    monkeypatch.setattr(client_module, "get_system_bus", lambda: bus)
    monkeypatch.setattr(client_module.dbus, "Interface", lambda value, _iface: value)
    client = AncsClient(
        "/device",
        lambda _event: None,
        previously_authorized=True,
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
        cancel=lambda _timer: None,
    )
    client._started = True
    client._bearer_connected = False
    client._ns_path = "/device/ns"
    client._ds_path = "/device/ds"
    client._cp_path = "/device/cp"
    client._owned_notify_paths = {"/device/ns", "/device/ds"}

    client.observe_bearer_state(True)
    scheduled.pop(0)[1]()

    assert calls == [("start", "ns"), "write"]
    assert client.subscribed is True
    assert client.authorized is False


def test_unknown_notifying_property_does_not_skip_start_notify(
    monkeypatch,
) -> None:
    scheduled = []
    calls = []

    class _Characteristic:
        def __init__(self, name: str) -> None:
            self.name = name

        @staticmethod
        def Get(_iface, _name, **_kwargs):
            raise client_module.dbus.exceptions.DBusException(
                "Unknown object",
                name="org.freedesktop.DBus.Error.UnknownObject",
            )

        def StartNotify(self, **_kwargs) -> None:
            calls.append(("start", self.name))

        def StopNotify(self, **_kwargs) -> None:
            calls.append(("stop", self.name))

        @staticmethod
        def WriteValue(_value, _options, **kwargs) -> None:
            calls.append("write")
            kwargs["reply_handler"]()

    ns = _Characteristic("ns")
    ds = _Characteristic("ds")
    cp = _Characteristic("cp")
    bus = _CharacteristicBus(
        {"/device/ns": ns, "/device/ds": ds, "/device/cp": cp}
    )
    monkeypatch.setattr(client_module, "get_system_bus", lambda: bus)
    monkeypatch.setattr(client_module.dbus, "Interface", lambda value, _iface: value)
    client = AncsClient(
        "/device",
        lambda _event: None,
        previously_authorized=True,
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
        cancel=lambda _timer: None,
    )
    client._started = True
    client._bearer_connected = False
    client._ns_path = "/device/ns"
    client._ds_path = "/device/ds"
    client._cp_path = "/device/cp"
    client._owned_notify_paths = {"/device/ns", "/device/ds"}

    client.observe_bearer_state(True)
    scheduled.pop(0)[1]()

    assert calls == [("start", "ns"), ("start", "ds"), "write"]
    assert client.subscribed is True
    assert client.authorized is False


def test_reconnect_control_point_failure_keeps_solicitation_needed(
    monkeypatch,
) -> None:
    scheduled = []
    resets = []

    class _Characteristic:
        @staticmethod
        def Get(_iface, name, **_kwargs):
            assert name == "Notifying"
            return True

        def StartNotify(self, **_kwargs) -> None:
            raise AssertionError("StartNotify should be skipped")

        def StopNotify(self, **_kwargs) -> None:
            raise AssertionError("StopNotify should be skipped")

        @staticmethod
        def WriteValue(_value, _options, **kwargs) -> None:
            kwargs["error_handler"](client_module.dbus.exceptions.DBusException(
                "Not connected",
                name="org.bluez.Error.Failed",
            ))

    bus = _CharacteristicBus(
        {
            "/device/ns": _Characteristic(),
            "/device/ds": _Characteristic(),
            "/device/cp": _Characteristic(),
        }
    )
    monkeypatch.setattr(client_module, "get_system_bus", lambda: bus)
    monkeypatch.setattr(client_module.dbus, "Interface", lambda value, _iface: value)
    client = AncsClient(
        "/device",
        lambda _event: None,
        previously_authorized=True,
        on_transport_failure=lambda: resets.append(True),
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
        cancel=lambda _timer: None,
    )
    client._started = True
    client._ns_path = "/device/ns"
    client._ds_path = "/device/ds"
    client._cp_path = "/device/cp"
    client._owned_notify_paths = {"/device/ns", "/device/ds"}
    client._was_authorized = True

    client.observe_bearer_state(True)
    scheduled.pop(0)[1]()

    assert client.subscribed is False
    assert client.authorized is False
    assert client.connected is False
    assert client._transport_blocked is True
    assert scheduled[0][0] == client_module.TRANSPORT_RESET_SECONDS
    assert resets == []


def test_not_connected_control_point_failure_invalidates_ancs_health(
    monkeypatch,
) -> None:
    statuses = []
    scheduled = []
    resets = []

    class _ControlPoint:
        @staticmethod
        def WriteValue(_value, _options, **kwargs) -> None:
            kwargs["error_handler"](client_module.dbus.exceptions.DBusException(
                "Not connected",
                name="org.bluez.Error.Failed",
            ))

    bus = _CharacteristicBus({"/device/cp": _ControlPoint()})
    monkeypatch.setattr(client_module, "get_system_bus", lambda: bus)
    monkeypatch.setattr(client_module.dbus, "Interface", lambda value, _iface: value)
    client = AncsClient(
        "/device",
        lambda _event: None,
        on_status=lambda: statuses.append(True),
        on_transport_failure=lambda: resets.append(True),
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
        cancel=lambda _timer: None,
    )
    client._started = True
    client._bearer_connected = True
    client._bearer_ready = True
    client._ns_path = "/device/ns"
    client._ds_path = "/device/ds"
    client._cp_path = "/device/cp"
    client._notify_started = True
    client._authorized = True
    client._was_authorized = True
    matches = [_Match(), _Match()]
    client._characteristic_signal_matches = matches

    client._request_attrs(Notification.parse(_notification(42)))

    assert client.connected is False
    assert client.subscribed is False
    assert client.authorized is False
    assert client._bearer_connected is True
    assert client._transport_blocked is True
    assert all(match.removed for match in matches)
    assert statuses == [True]
    assert scheduled[0][0] == client_module.TRANSPORT_RESET_SECONDS

    client.observe_bearer_state(True)
    assert client.subscribed is False
    assert len(scheduled) == 1

    scheduled[0][1]()
    assert resets == [True]


@pytest.mark.parametrize("bearer_ready", [True, False])
def test_authorization_timeout_resets_previously_authorized_transport(
    monkeypatch, bearer_ready,
) -> None:
    scheduled = []
    resets = []
    retired = []

    class _ControlPoint:
        @staticmethod
        def WriteValue(_value, _options, **kwargs) -> None:
            kwargs["reply_handler"]()

    bus = _CharacteristicBus({"/device/cp": _ControlPoint()})
    monkeypatch.setattr(client_module, "get_system_bus", lambda: bus)
    monkeypatch.setattr(client_module.dbus, "Interface", lambda value, _iface: value)
    client = AncsClient(
        "/device",
        lambda _event: None,
        on_transport_failure=lambda: resets.append(True),
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
        cancel=lambda _timer: None,
    )
    client._started = True
    client._notify_started = True
    client._bearer_connected = True
    client._bearer_ready = True
    client._cp_path = "/device/cp"

    client._queue_authorization_probe()
    _complete_authorization_probe(client)
    client._authorized = False
    client._queue_authorization_probe()
    client._bearer_ready = bearer_ready
    monkeypatch.setattr(client, "_stop_bluez_notifications", lambda: retired.append(True))
    client._request_timed_out()

    assert retired == ([True] if bearer_ready else [])
    assert client.connected is False
    assert client.subscribed is False
    assert client._transport_blocked is True
    # The probe's own timeout goes through the injected scheduler as well;
    # the reset is the timer armed by the timeout handling.
    assert (client_module.REQUEST_TIMEOUT_SECONDS, client._request_timed_out) in scheduled
    assert scheduled[-1] == (
        client_module.TRANSPORT_RESET_SECONDS, client._request_transport_reset,
    )

    scheduled[-1][1]()
    assert resets == [True]


def test_initial_authorization_timeout_keeps_retrying_without_transport_reset(
    monkeypatch,
) -> None:
    scheduled = []
    resets = []

    class _ControlPoint:
        @staticmethod
        def WriteValue(_value, _options, **kwargs) -> None:
            kwargs["reply_handler"]()

    bus = _CharacteristicBus({"/device/cp": _ControlPoint()})
    monkeypatch.setattr(client_module, "get_system_bus", lambda: bus)
    monkeypatch.setattr(client_module.dbus, "Interface", lambda value, _iface: value)
    client = AncsClient(
        "/device",
        lambda _event: None,
        on_transport_failure=lambda: resets.append(True),
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
        cancel=lambda _timer: None,
    )
    client._started = True
    client._notify_started = True
    client._bearer_connected = True
    client._bearer_ready = True
    client._cp_path = "/device/cp"

    client._queue_authorization_probe()
    client._request_timed_out()

    assert client.subscribed is True
    assert client._transport_blocked is False
    assert scheduled == [
        (client_module.REQUEST_TIMEOUT_SECONDS, client._request_timed_out),
        (client_module.AUTHORIZATION_RETRY_SECONDS, client._retry_authorization),
    ]
    assert resets == []


def test_request_timeout_is_armed_and_cancelled_through_injected_timers(
    monkeypatch,
) -> None:
    scheduled = []
    cancelled = []

    class _ControlPoint:
        @staticmethod
        def WriteValue(_value, _options, **kwargs) -> None:
            kwargs["reply_handler"]()

    bus = _CharacteristicBus({"/device/cp": _ControlPoint()})
    monkeypatch.setattr(client_module, "get_system_bus", lambda: bus)
    monkeypatch.setattr(client_module.dbus, "Interface", lambda value, _iface: value)
    client = AncsClient(
        "/device",
        lambda _event: None,
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 41,
        cancel=cancelled.append,
    )
    client._started = True
    client._notify_started = True
    client._bearer_connected = True
    client._bearer_ready = True
    client._cp_path = "/device/cp"

    client._queue_authorization_probe()
    assert scheduled == [
        (client_module.REQUEST_TIMEOUT_SECONDS, client._request_timed_out),
    ]

    _complete_authorization_probe(client)
    assert client.authorized is True
    assert cancelled == [41]
    assert client._request_timeout_id is None


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
    client._bearer_connected = True
    client._bearer_ready = True
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
        def WriteValue(_value, _options, **kwargs) -> None:
            client._bluez_owner_generation += 1
            client._request_queue.clear()
            client._active_request = replacement_request
            client._request_timeout_id = 91
            kwargs["reply_handler"]()

    bus = _CharacteristicBus({"/device/cp": _ControlPoint()})
    monkeypatch.setattr(client_module, "get_system_bus", lambda: bus)
    monkeypatch.setattr(client_module.dbus, "Interface", lambda value, _iface: value)
    client = AncsClient(
        "/device",
        lambda _event: None,
        schedule=lambda *args: timeout_calls.append(args) or 92,
        cancel=lambda _timer: None,
    )
    client._started = True
    client._notify_started = True
    client._bearer_connected = True
    client._bearer_ready = True
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

        def WriteValue(self, _value, _options, **kwargs) -> None:
            if self.fail:
                self.fail = False
                kwargs["error_handler"](client_module.dbus.exceptions.DBusException(
                    "Insufficient authorization",
                    name="org.bluez.Error.Failed",
                ))
                return
            kwargs["reply_handler"]()

    cp = _ControlPoint()
    bus = _CharacteristicBus({"/device/cp": cp})
    monkeypatch.setattr(client_module, "get_system_bus", lambda: bus)
    monkeypatch.setattr(client_module.dbus, "Interface", lambda value, _iface: value)
    client = AncsClient(
        "/device",
        lambda _event: None,
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
        cancel=lambda _timer: None,
    )
    client._started = True
    client._notify_started = True
    client._bearer_connected = True
    client._bearer_ready = True
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


# ---- asynchronous Control Point writes -------------------------------------

class _PendingControlPoint:
    """Accepts WriteValue calls and leaves their completion to the test."""

    def __init__(self) -> None:
        self.writes: list[dict] = []

    def WriteValue(self, value, options, **kwargs) -> None:
        assert callable(kwargs.get("reply_handler"))
        assert callable(kwargs.get("error_handler"))
        # Marshal exactly as dbus-python does on a proxy without
        # introspection: a bare {} cannot be typed and would raise here.
        client_module.dbus.lowlevel.MethodCallMessage(
            "org.bluez", "/dev/cp", "org.bluez.GattCharacteristic1", "WriteValue",
        ).append(value, options)
        self.writes.append({"value": bytes(value), **kwargs})


def _async_write_client(monkeypatch, control_point):
    timers = []
    bus = _CharacteristicBus({"/device/cp": control_point})
    monkeypatch.setattr(client_module, "get_system_bus", lambda: bus)
    monkeypatch.setattr(client_module.dbus, "Interface", lambda value, _iface: value)
    # Every ANCS timer, including the request timeout, goes through the
    # injected scheduler; no real GLib source is ever armed.
    client = AncsClient(
        "/device",
        lambda _event: None,
        schedule=lambda delay, callback: timers.append((delay, callback)) or len(timers),
        cancel=lambda _source: None,
    )
    client._started = True
    client._notify_started = True
    client._authorized = True
    client._bearer_connected = True
    client._bearer_ready = True
    client._cp_path = "/device/cp"
    return client, timers


def _app_probe_response(uid: int, app_id: str) -> bytes:
    return (
        bytes([CommandID.GetNotificationAttributes])
        + struct.pack("<I", uid)
        + _attribute(0, app_id)
    )


def test_control_point_write_returns_before_bluez_replies(monkeypatch) -> None:
    cp = _PendingControlPoint()
    client, timers = _async_write_client(monkeypatch, cp)

    client._request_attrs(Notification.parse(_notification(1)))
    client._request_attrs(Notification.parse(_notification(2)))

    # One write in flight, the second request still queued behind it, and
    # the response timer not armed until BlueZ confirms the write.
    assert [write["value"][1:5] for write in cp.writes] == [struct.pack("<I", 1)]
    assert len(client._request_queue) == 1
    assert timers == []

    cp.writes[0]["reply_handler"]()
    assert [callback for _delay, callback in timers] == [client._request_timed_out]
    assert timers[0][0] == client_module.REQUEST_TIMEOUT_SECONDS
    assert len(cp.writes) == 1

    client._on_ds_changed(
        "org.bluez.GattCharacteristic1",
        {"Value": _app_probe_response(1, "com.example.Private")},
        [],
    )
    assert [write["value"][1:5] for write in cp.writes] == [
        struct.pack("<I", 1), struct.pack("<I", 2),
    ]


def test_async_write_failure_releases_the_queue(monkeypatch) -> None:
    cp = _PendingControlPoint()
    client, _timers = _async_write_client(monkeypatch, cp)
    client._request_attrs(Notification.parse(_notification(1)))
    client._request_attrs(Notification.parse(_notification(2)))

    cp.writes[0]["error_handler"](client_module.dbus.exceptions.DBusException(
        "Operation failed with ATT error: 0x0e", name="org.bluez.Error.Failed",
    ))

    assert len(cp.writes) == 2
    assert client.connected is True


def test_async_write_losing_the_link_latches_transport_failure(monkeypatch) -> None:
    cp = _PendingControlPoint()
    client, _timers = _async_write_client(monkeypatch, cp)
    client._request_attrs(Notification.parse(_notification(1)))

    cp.writes[0]["error_handler"](client_module.dbus.exceptions.DBusException(
        "Not connected", name="org.bluez.Error.Failed",
    ))

    assert client.connected is False
    assert client._transport_blocked is True


def test_write_that_fails_before_dispatch_is_handled_like_a_reply(monkeypatch) -> None:
    class _ClosedBus:
        writes = 0

        def WriteValue(self, _value, _options, **_kwargs) -> None:
            self.writes += 1
            if self.writes == 1:
                raise client_module.dbus.exceptions.DBusException(
                    "connection closed",
                    name="org.freedesktop.DBus.Error.Disconnected",
                )

    cp = _ClosedBus()
    client, _timers = _async_write_client(monkeypatch, cp)
    client._request_attrs(Notification.parse(_notification(1)))
    client._request_attrs(Notification.parse(_notification(2)))

    assert cp.writes == 2
    assert client._active_request.notification.id == 2


@pytest.mark.parametrize("failure", [ValueError, TypeError])
def test_programming_error_before_dispatch_surfaces_and_releases_the_request(
    monkeypatch, failure,
) -> None:
    class _Unmarshallable:
        writes = 0

        def WriteValue(self, _value, _options, **_kwargs) -> None:
            self.writes += 1
            if self.writes == 1:
                raise failure("Unable to guess signature from an empty dict")

    cp = _Unmarshallable()
    client, timers = _async_write_client(monkeypatch, cp)
    # Not disguised as a BlueZ InvalidArgs warning: the bug propagates.
    with pytest.raises(failure):
        client._request_attrs(Notification.parse(_notification(1)))
    assert client._active_request is None
    assert client._cp_write_token is None

    # ...but the queue is not wedged by it.
    client._request_attrs(Notification.parse(_notification(2)))
    assert cp.writes == 2
    assert client._active_request.notification.id == 2
    assert client.connected is True
    assert timers == []


class _BlueZWriteOpControlPoint(_PendingControlPoint):
    """Models gatt-client.c: a second write while write_op is set is refused."""

    def __init__(self) -> None:
        super().__init__()
        self.write_op = False
        self.rejected = 0

    def WriteValue(self, value, options, **kwargs) -> None:
        if self.write_op:
            self.rejected += 1
            kwargs["error_handler"](client_module.dbus.exceptions.DBusException(
                "In Progress", name="org.bluez.Error.InProgress",
            ))
            return
        self.write_op = True
        super().WriteValue(value, options, **kwargs)

    def reply(self, index: int) -> None:
        self.write_op = False
        self.writes[index]["reply_handler"]()

    def fail(self, index: int, error) -> None:
        self.write_op = False
        self.writes[index]["error_handler"](error)


def _uids(cp) -> list[int]:
    return [struct.unpack("<I", write["value"][1:5])[0] for write in cp.writes]


def test_reassembly_failure_does_not_overlap_an_unacknowledged_write(
    monkeypatch,
) -> None:
    cp = _BlueZWriteOpControlPoint()
    client, _timers = _async_write_client(monkeypatch, cp)
    for uid in (1, 2, 3, 4):
        client._request_attrs(Notification.parse(_notification(uid)))
    assert _uids(cp) == [1]

    # A late fragment from an earlier, timed-out request breaks reassembly
    # of request 1 while its write is still unacknowledged.
    client._on_ds_changed(
        "org.bluez.GattCharacteristic1", {"Value": b"\x07garbage"}, [],
    )
    assert client._active_request is None
    assert cp.rejected == 0
    assert _uids(cp) == [1]

    # Request 2 goes out only once BlueZ has answered write 1.
    cp.reply(0)
    assert _uids(cp) == [1, 2]
    for index, uid in ((1, 2), (2, 3)):
        cp.reply(index)
        client._on_ds_changed(
            "org.bluez.GattCharacteristic1",
            {"Value": _app_probe_response(uid, "com.example.Private")},
            [],
        )
    assert _uids(cp) == [1, 2, 3, 4]
    assert cp.rejected == 0


def test_response_before_write_reply_waits_for_the_reply(monkeypatch) -> None:
    cp = _BlueZWriteOpControlPoint()
    client, timers = _async_write_client(monkeypatch, cp)
    client._request_attrs(Notification.parse(_notification(1)))
    client._request_attrs(Notification.parse(_notification(2)))

    client._on_ds_changed(
        "org.bluez.GattCharacteristic1",
        {"Value": _app_probe_response(1, "com.example.Private")},
        [],
    )
    assert _uids(cp) == [1]
    cp.reply(0)

    assert timers == []
    assert _uids(cp) == [1, 2]
    assert cp.rejected == 0
    assert client._active_request.notification.id == 2


def test_no_reply_after_a_completed_request_releases_the_control_point(
    monkeypatch,
) -> None:
    cp = _BlueZWriteOpControlPoint()
    client, _timers = _async_write_client(monkeypatch, cp)
    client._request_attrs(Notification.parse(_notification(1)))
    client._request_attrs(Notification.parse(_notification(2)))
    client._on_ds_changed(
        "org.bluez.GattCharacteristic1",
        {"Value": _app_probe_response(1, "com.example.Private")},
        [],
    )

    cp.fail(0, client_module.dbus.exceptions.DBusException(
        "Did not receive a reply", name="org.freedesktop.DBus.Error.NoReply",
    ))

    assert _uids(cp) == [1, 2]
    assert client.connected is True


def test_no_reply_for_the_active_request_abandons_only_that_request(
    monkeypatch,
) -> None:
    cp = _BlueZWriteOpControlPoint()
    client, timers = _async_write_client(monkeypatch, cp)
    client._request_attrs(Notification.parse(_notification(1)))
    client._request_attrs(Notification.parse(_notification(2)))

    cp.fail(0, client_module.dbus.exceptions.DBusException(
        "Did not receive a reply", name="org.freedesktop.DBus.Error.NoReply",
    ))

    assert _uids(cp) == [1, 2]
    assert client._active_request.notification.id == 2
    assert timers == []
    assert client.connected is True


def test_in_progress_from_another_writer_is_retried(monkeypatch) -> None:
    cp = _BlueZWriteOpControlPoint()
    cp.write_op = True  # another D-Bus client is mid-write
    client, timers = _async_write_client(monkeypatch, cp)
    client._request_attrs(Notification.parse(_notification(1)))

    assert cp.rejected == 1
    assert client._active_request is None
    assert [delay for delay, _callback in timers] == [
        client_module.CONTROL_POINT_BUSY_RETRY_SECONDS,
    ]
    cp.write_op = False
    timers[0][1]()
    assert _uids(cp) == [1]
    assert client._active_request.notification.id == 1


def test_in_progress_retries_are_bounded(monkeypatch) -> None:
    cp = _BlueZWriteOpControlPoint()
    cp.write_op = True
    client, timers = _async_write_client(monkeypatch, cp)
    client._request_attrs(Notification.parse(_notification(1)))
    for _ in range(client_module.MAX_CONTROL_POINT_BUSY_RETRIES):
        timers[-1][1]()

    assert cp.rejected == client_module.MAX_CONTROL_POINT_BUSY_RETRIES + 1
    assert client._active_request is None
    assert not client._request_queue
    assert client._cp_busy_retry_id is None
    # The key was released, so the notification can be requested again.
    assert client._request_queue.enqueue("notification:1", object())


def test_late_callbacks_after_stop_are_ignored(monkeypatch) -> None:
    cp = _BlueZWriteOpControlPoint()
    client, timers = _async_write_client(monkeypatch, cp)
    client._request_attrs(Notification.parse(_notification(1)))
    client.stop()

    cp.reply(0)
    cp.writes[0]["error_handler"](client_module.dbus.exceptions.DBusException(
        "late", name="org.bluez.Error.Failed",
    ))

    assert timers == []
    assert client._active_request is None
    assert client._cp_write_token is None
    assert len(cp.writes) == 1


def test_reset_keeps_the_next_write_behind_an_unacknowledged_one(
    monkeypatch,
) -> None:
    cp = _BlueZWriteOpControlPoint()
    client, timers = _async_write_client(monkeypatch, cp)
    client._request_attrs(Notification.parse(_notification(1)))
    client._reset_requests()
    client._request_attrs(Notification.parse(_notification(2)))
    assert _uids(cp) == [1]

    cp.reply(0)  # stale for request 1, but frees the characteristic
    assert _uids(cp) == [1, 2]
    assert cp.rejected == 0
    assert timers == []
    cp.reply(1)
    assert [callback for _delay, callback in timers] == [client._request_timed_out]


def test_owner_change_drops_the_old_daemons_pending_write(monkeypatch) -> None:
    cp = _BlueZWriteOpControlPoint()
    client, _timers = _async_write_client(monkeypatch, cp)
    client._request_attrs(Notification.parse(_notification(1)))
    monkeypatch.setattr(client, "_bind_manager_and_rescan", lambda: None)
    client.observe_bluez_owner(":1.1", ":1.2")
    client._cp_path = "/device/cp"
    cp.write_op = False  # the new bluetoothd has no write_op
    client._request_attrs(Notification.parse(_notification(2)))
    assert _uids(cp) == [1, 2]

    # The old daemon's late reply neither arms a timer nor frees write 2.
    cp.writes[0]["reply_handler"]()
    assert client._cp_write_token is not None
    assert client._active_request.notification.id == 2

# ---- opt-in notification actions ------------------------------------------

_BOTH_ACTIONS = EventFlag.PositiveAction | EventFlag.NegativeAction


def _complete_labelled_response(
    client: AncsClient, uid: int, app_id: str, *labels: tuple[int, str],
) -> None:
    request = client._request_queue.popleft()
    client._active_request = request
    response = (
        bytes([CommandID.GetNotificationAttributes])
        + struct.pack("<I", uid)
        + _attribute(0, app_id)
        + _attribute(1, "Alice")
        + _attribute(2, "")
        + _attribute(3, "Incoming call")
    )
    for attribute_id, value in labels:
        response += _attribute(attribute_id, value)
    client._on_ds_changed(
        "org.bluez.GattCharacteristic1", {"Value": response}, []
    )


class _AsyncControlPoint:
    """Records asynchronous WriteValue calls without completing them."""

    def __init__(self) -> None:
        self.writes: list[dict] = []

    def WriteValue(self, value, options, **kwargs) -> None:
        assert "reply_handler" in kwargs and "error_handler" in kwargs
        # Marshal exactly as dbus-python does on a proxy without
        # introspection: a bare {} cannot be typed and would raise here.
        client_module.dbus.lowlevel.MethodCallMessage(
            "org.bluez", "/dev/cp", "org.bluez.GattCharacteristic1", "WriteValue",
        ).append(value, options)
        self.writes.append({"value": bytes(value), "options": options, **kwargs})


class _ActionBus:
    def __init__(self, control_point) -> None:
        self.control_point = control_point

    def get_object(self, _name, path, introspect=True):
        assert path == "/device/cp"
        return self.control_point


def _action_client(monkeypatch, *, enabled=True, removed=None, reset=None):
    emitted = []
    cp = _AsyncControlPoint()
    monkeypatch.setattr(client_module, "get_system_bus", lambda: _ActionBus(cp))
    monkeypatch.setattr(client_module.dbus, "Interface", lambda value, _iface: value)
    client = AncsClient(
        "/device",
        emitted.append,
        include_non_message_notifications=lambda: True,
        notification_actions=enabled,
        on_notification_removed=removed,
        on_actions_reset=reset,
        schedule=lambda _delay, _callback: 5,
        cancel=lambda _source: None,
    )
    client._started = True
    client._notify_started = True
    client._authorized = True
    client._bearer_connected = True
    client._bearer_ready = True
    client._cp_path = "/device/cp"
    client._app_name_cache["com.apple.mobilephone"] = "Phone"
    # Attribute requests are completed by hand below; actions must not need
    # the serialized request pump at all.
    monkeypatch.setattr(client, "_pump_requests", lambda: None)
    return client, cp, emitted


def _deliver_call(client, uid=42, flags=_BOTH_ACTIONS, labels=((6, "Accept"), (7, "Decline"))):
    client._request_attrs(Notification.parse(_notification(uid, flags=flags)))
    _complete_app_probe(client, uid, "com.apple.mobilephone")
    _complete_labelled_response(client, uid, "com.apple.mobilephone", *labels)


def test_disabled_actions_request_no_labels_even_for_flagged_apps(monkeypatch) -> None:
    client, cp, _emitted = _action_client(monkeypatch, enabled=False)
    client._request_attrs(Notification.parse(_notification(42, flags=_BOTH_ACTIONS)))
    _complete_app_probe(client, 42, "com.apple.mobilephone")

    request = client._request_queue.popleft()
    assert request.assembler.attribute_ids == (0, 1, 2, 3)
    assert request.packet == client_module.build_get_notification_attributes(42)

    results = []
    assert client.perform_notification_action(42, True, results.append) is False
    assert results == [client_module.ACTION_UNAVAILABLE]
    assert cp.writes == []


def test_enabled_actions_request_only_announced_labels(monkeypatch) -> None:
    client, _cp, _emitted = _action_client(monkeypatch)
    client._request_attrs(
        Notification.parse(_notification(42, flags=EventFlag.NegativeAction))
    )
    _complete_app_probe(client, 42, "com.example.Calendar")

    request = client._request_queue.popleft()
    assert request.assembler.attribute_ids == (0, 1, 2, 3, 7)
    assert request.packet.endswith(bytes([7]))


def test_enabled_actions_never_request_labels_for_messages(monkeypatch) -> None:
    client, _cp, _emitted = _action_client(monkeypatch)
    client._request_attrs(Notification.parse(_notification(42, flags=_BOTH_ACTIONS)))
    _complete_app_probe(client, 42, MESSAGES_APP_ID)

    assert client._request_queue.popleft().assembler.attribute_ids == (0, 1, 2, 3)


def test_labels_reach_the_event_and_positive_click_writes_action(monkeypatch) -> None:
    client, cp, emitted = _action_client(monkeypatch)
    _deliver_call(client)

    assert emitted[0].positive_action_label == "Accept"
    assert emitted[0].negative_action_label == "Decline"
    results = []

    assert client.perform_notification_action(42, True, results.append) is True

    assert len(cp.writes) == 1
    assert cp.writes[0]["value"] == bytes.fromhex("02" "2a000000" "00")
    assert results == []  # asynchronous: nothing reported until BlueZ replies
    cp.writes[0]["reply_handler"]()
    assert results == [client_module.ACTION_SENT]


def test_action_is_single_use_per_notification(monkeypatch) -> None:
    client, cp, _emitted = _action_client(monkeypatch)
    _deliver_call(client)
    results = []

    client.perform_notification_action(42, False, results.append)
    cp.writes[0]["reply_handler"]()
    client.perform_notification_action(42, False, results.append)
    client.perform_notification_action(42, True, results.append)

    assert len(cp.writes) == 1
    assert cp.writes[0]["value"][-1] == 1
    assert results == [
        client_module.ACTION_SENT,
        client_module.ACTION_UNAVAILABLE,
        client_module.ACTION_UNAVAILABLE,
    ]


def test_unannounced_action_is_refused_without_writing(monkeypatch) -> None:
    client, cp, _emitted = _action_client(monkeypatch)
    _deliver_call(client, flags=EventFlag.PositiveAction, labels=((6, "Accept"),))
    results = []

    assert client.perform_notification_action(42, False, results.append) is False
    assert client.perform_notification_action(99, True, results.append) is False
    assert results == [client_module.ACTION_UNAVAILABLE] * 2
    assert cp.writes == []


def test_empty_labels_do_not_make_a_notification_actionable(monkeypatch) -> None:
    client, cp, emitted = _action_client(monkeypatch)
    _deliver_call(client, labels=((6, ""), (7, "")))

    assert emitted[0].has_actions is False
    assert client.perform_notification_action(42, True) is False
    assert cp.writes == []


def _dbus_error(message: str, name: str = "org.bluez.Error.Failed"):
    return client_module.dbus.exceptions.DBusException(message, name=name)


@pytest.mark.parametrize(
    "error,result",
    [
        (_dbus_error("Operation failed with ATT error: 0xa2"),
         client_module.ACTION_UNAVAILABLE),
        (_dbus_error("Operation failed with ATT error: 0xa3"),
         client_module.ACTION_REJECTED),
        (_dbus_error("Operation failed with ATT error: 0xa0"),
         client_module.ACTION_UNSUPPORTED),
        (_dbus_error("Operation failed with ATT error: 0xa1"),
         client_module.ACTION_UNSUPPORTED),
        (_dbus_error("Did not receive a reply",
                     "org.freedesktop.DBus.Error.NoReply"),
         client_module.ACTION_FAILED),
    ],
)
def test_action_write_errors_map_to_results(monkeypatch, error, result) -> None:
    client, cp, _emitted = _action_client(monkeypatch)
    _deliver_call(client)
    results = []
    client.perform_notification_action(42, True, results.append)

    cp.writes[0]["error_handler"](error)

    assert results == [result]
    assert client.connected is True
    assert not client._actions_in_flight


def test_lost_connection_during_action_invalidates_transport(monkeypatch) -> None:
    status = []
    client, cp, _emitted = _action_client(monkeypatch)
    client.on_status = lambda: status.append(client.connected)
    _deliver_call(client)
    results = []
    client.perform_notification_action(42, True, results.append)

    cp.writes[0]["error_handler"](_dbus_error("Not connected"))

    assert results == [client_module.ACTION_DISCONNECTED]
    assert client.connected is False
    assert status == [False]


def test_action_while_disconnected_is_refused(monkeypatch) -> None:
    client, cp, _emitted = _action_client(monkeypatch)
    _deliver_call(client)
    client._authorized = False
    results = []

    assert client.perform_notification_action(42, True, results.append) is False
    assert results == [client_module.ACTION_DISCONNECTED]
    assert cp.writes == []


def test_concurrent_actions_are_bounded(monkeypatch) -> None:
    client, cp, _emitted = _action_client(monkeypatch)
    limit = client_module.MAX_ANCS_ACTIONS_IN_FLIGHT
    for uid in range(1, limit + 2):
        _deliver_call(client, uid=uid)
    results = []

    for uid in range(1, limit + 2):
        client.perform_notification_action(uid, True, results.append)

    assert len(cp.writes) == limit
    assert results == [client_module.ACTION_BUSY]
    cp.writes[0]["reply_handler"]()
    assert client.perform_notification_action(limit + 1, True, results.append)


def test_phone_removal_retires_actions_and_reports_only_the_uid(monkeypatch) -> None:
    removed = []
    client, cp, _emitted = _action_client(monkeypatch, removed=removed.append)
    _deliver_call(client)
    client._request_attrs(Notification.parse(_notification(43)))
    _complete_app_probe(client, 43, "com.apple.mobilephone")
    _complete_labelled_response(client, 43, "com.apple.mobilephone")

    for uid in (43, 42):
        client._on_ns_changed(
            "org.bluez.GattCharacteristic1",
            {"Value": struct.pack(
                "<BBBBI", EventID.NotificationRemoved, 0, 1, 0, uid
            )},
            [],
        )

    # 43 never offered an action, so no desktop popup needs closing.
    assert removed == [42]
    assert client.perform_notification_action(42, True) is False
    assert cp.writes == []


def test_session_reset_invalidates_previous_uids(monkeypatch) -> None:
    client, cp, _emitted = _action_client(monkeypatch)
    _deliver_call(client)
    client.perform_notification_action(42, True)
    pending = cp.writes[-1]
    _deliver_call(client, uid=7)

    client.observe_bearer_state(False)
    client.observe_bearer_state(True)
    client._notify_started = True
    client._authorized = True
    client._bearer_ready = True
    results = []

    assert client.perform_notification_action(7, True, results.append) is False
    assert results == [client_module.ACTION_UNAVAILABLE]
    # A late reply from the old session still reports to its caller safely.
    pending["reply_handler"]()


def test_session_reset_tells_the_desktop_to_retire_old_buttons(monkeypatch) -> None:
    resets = []
    client, _cp, _emitted = _action_client(
        monkeypatch, reset=lambda: resets.append(True)
    )
    _deliver_call(client)

    client.observe_bearer_state(False)

    assert resets == [True]
    assert client.perform_notification_action(42, True) is False


def test_action_write_options_marshal_without_introspection() -> None:
    import dbus.lowlevel

    message = dbus.lowlevel.MethodCallMessage(
        "org.bluez", "/dev/cp", "org.bluez.GattCharacteristic1", "WriteValue",
    )
    with pytest.raises(ValueError):
        message.append([client_module.dbus.Byte(0)], {})


def test_disabled_actions_never_report_an_actions_reset(monkeypatch) -> None:
    resets = []
    client, _cp, _emitted = _action_client(
        monkeypatch, enabled=False, reset=lambda: resets.append(True)
    )

    client.observe_bearer_state(False)
    client._reset_actions()

    assert resets == []
