"""Shared graphical pairing workflow regressions."""

from __future__ import annotations

import logging
import stat

import pytest

from blueferry import bluez_setup, config, pair_setup
from blueferry.bluetooth_devices import iphone_candidates

_BLUEZ_DEVICE_SNAPSHOT = pair_setup._bluez_device_snapshot


@pytest.fixture(autouse=True)
def _pairing_reports_in_tmp(tmp_path, monkeypatch):
    state = tmp_path / "blueferry-state"
    monkeypatch.setattr(config, "STATE_DIR", state)
    monkeypatch.setattr(config, "EVENTS_DB", state / "events.sqlite")
    monkeypatch.setattr(config, "CONTACTS_DB", state / "contacts.sqlite")
    monkeypatch.setattr(pair_setup, "_adapter_identity", lambda adapter, *_args, **_kwargs: {"name": adapter})
    monkeypatch.setattr(
        pair_setup,
        "_le_bearer_snapshot",
        lambda _path: {
            "present": False, "paired": False, "bonded": False, "connected": False,
        },
    )
    pair_setup._pending_teardown_traces.clear()
    monkeypatch.setattr(
        pair_setup,
        "_bluez_device_snapshot",
        lambda _path: {"device_present": False},
    )


def _device(*, paired: bool) -> pair_setup.PairedDevice:
    return pair_setup.PairedDevice(
        mac="02:00:00:00:00:01",
        name="Test iPhone",
        icon="phone",
        trusted=paired,
        connected=paired,
        paired=paired,
        adapter_path="/org/bluez/hci0",
        device_path="/org/bluez/hci0/dev_02_00_00_00_00_01",
        uuids=frozenset({config.ANCS_SOLICIT_UUID}) if paired else frozenset(),
        services_resolved=paired,
    )


def test_discovery_stops_as_soon_as_an_iphone_appears(monkeypatch):
    phone = _device(paired=False)
    scans = [[], [], [phone]]
    sleeps = []
    elapsed = 0.0

    class Adapter:
        def Set(self, *_args):
            pass

        def StartDiscovery(self):
            pass

        def StopDiscovery(self):
            pass

    class ObjectManager:
        def GetManagedObjects(self):
            return {"/org/bluez/hci0": {"org.bluez.Adapter1": {}}}

    class Bus:
        def get_object(self, *_args):
            return Adapter()

    def sleep(seconds):
        nonlocal elapsed
        sleeps.append(seconds)
        elapsed += seconds

    monkeypatch.setattr(pair_setup, "_object_manager", ObjectManager)
    monkeypatch.setattr(pair_setup, "get_system_bus", Bus)
    monkeypatch.setattr(pair_setup.dbus, "Interface", lambda value, _iface: value)
    monkeypatch.setattr(pair_setup, "list_devices", lambda: scans.pop(0))
    monkeypatch.setattr(pair_setup.time, "monotonic", lambda: elapsed)
    monkeypatch.setattr(pair_setup.time, "sleep", sleep)

    assert pair_setup.discover_devices(8) == [phone]
    assert sleeps == [0.25, 0.25]
    assert elapsed < 8


def test_discover_devices_starts_on_the_requested_adapter(monkeypatch):
    started = []
    stopped = []

    class Adapter:
        def __init__(self, path):
            self.path = path

        def Set(self, *_args):
            pass

        def StartDiscovery(self):
            started.append(self.path)

        def StopDiscovery(self):
            stopped.append(self.path)

    class ObjectManager:
        def GetManagedObjects(self):
            return {
                "/org/bluez/hci0": {"org.bluez.Adapter1": {}},
                "/org/bluez/hci1": {"org.bluez.Adapter1": {}},
            }

    class Bus:
        def get_object(self, _service, path):
            return Adapter(path)

    monkeypatch.setattr(pair_setup, "_object_manager", ObjectManager)
    monkeypatch.setattr(pair_setup, "get_system_bus", Bus)
    monkeypatch.setattr(pair_setup.dbus, "Interface", lambda value, _iface: value)
    monkeypatch.setattr(pair_setup, "list_devices", lambda: [])
    now = iter([0.0, 2.0])
    monkeypatch.setattr(pair_setup.time, "monotonic", lambda: next(now, 2.0))

    pair_setup.discover_devices(1, adapter="hci1")

    assert started == ["/org/bluez/hci1"]
    assert "/org/bluez/hci0" in stopped
    assert "/org/bluez/hci1" in stopped


def test_discover_devices_waits_for_an_iphone_on_the_requested_adapter(monkeypatch):
    leftover = _device(paired=False)
    phone = pair_setup.PairedDevice(
        mac="02:00:00:00:00:02",
        name="Test iPhone",
        icon="phone",
        trusted=False,
        connected=False,
        paired=False,
        adapter_path="/org/bluez/hci1",
        device_path="/org/bluez/hci1/dev_02_00_00_00_00_02",
        uuids=frozenset(),
        services_resolved=False,
    )
    scans = [[leftover], [leftover], [leftover, phone]]
    sleeps = []
    elapsed = 0.0

    class Adapter:
        def Set(self, *_args):
            pass

        def StartDiscovery(self):
            pass

        def StopDiscovery(self):
            pass

    class ObjectManager:
        def GetManagedObjects(self):
            return {
                "/org/bluez/hci0": {"org.bluez.Adapter1": {}},
                "/org/bluez/hci1": {"org.bluez.Adapter1": {}},
            }

    class Bus:
        def get_object(self, *_args):
            return Adapter()

    def sleep(seconds):
        nonlocal elapsed
        sleeps.append(seconds)
        elapsed += seconds

    monkeypatch.setattr(pair_setup, "_object_manager", ObjectManager)
    monkeypatch.setattr(pair_setup, "get_system_bus", Bus)
    monkeypatch.setattr(pair_setup.dbus, "Interface", lambda value, _iface: value)
    monkeypatch.setattr(pair_setup, "list_devices", lambda: scans.pop(0))
    monkeypatch.setattr(pair_setup.time, "monotonic", lambda: elapsed)
    monkeypatch.setattr(pair_setup.time, "sleep", sleep)

    assert pair_setup.discover_devices(8, adapter="hci1") == [phone]
    assert sleeps == [0.25, 0.25]
    assert elapsed < 8


def test_discover_devices_rejects_a_missing_requested_adapter(monkeypatch):
    class ObjectManager:
        def GetManagedObjects(self):
            return {"/org/bluez/hci0": {"org.bluez.Adapter1": {}}}

    monkeypatch.setattr(pair_setup, "_object_manager", ObjectManager)

    with pytest.raises(pair_setup.PairingError, match="hci1"):
        pair_setup.discover_devices(1, adapter="hci1")


def test_discover_devices_rejects_an_invalid_adapter_name():
    with pytest.raises(pair_setup.PairingError, match="invalid Bluetooth adapter"):
        pair_setup.discover_devices(1, adapter="not an adapter")


def test_write_local_env_preserves_settings_and_is_private(tmp_path, monkeypatch):
    destination = tmp_path / "blueferry" / "local.env"
    destination.parent.mkdir()
    destination.write_text(
        "BLUEFERRY_MAC=00:00:00:00:00:00\n"
        "BLUEFERRY_SHOW_NOTIFICATION_CONTENT=false\n"
        "LD_PRELOAD=/tmp/not-allowed.so\n"
    )
    monkeypatch.setattr(pair_setup, "LOCAL_ENV_PATH", destination)

    pair_setup.write_local_env("02:00:00:00:00:01", "hci7")

    assert destination.read_text() == (
        "BLUEFERRY_MAC=02:00:00:00:00:01\n"
        "BLUEFERRY_ADAPTER=hci7\n"
        "BLUEFERRY_ANCS_ENABLED=true\n"
        "BLUEFERRY_SHOW_NOTIFICATION_CONTENT=false\n"
    )
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


def test_write_local_env_rejects_environment_injection(tmp_path, monkeypatch):
    monkeypatch.setattr(pair_setup, "LOCAL_ENV_PATH", tmp_path / "local.env")

    with pytest.raises(pair_setup.PairingError):
        pair_setup.write_local_env("02:00:00:00:00:01\nEVIL=1", "hci0")
    with pytest.raises(pair_setup.PairingError):
        pair_setup.write_local_env("02:00:00:00:00:01", "hci0\nEVIL=1")


def test_write_local_env_persists_compatibility_ancs_policy(tmp_path, monkeypatch):
    destination = tmp_path / "local.env"
    monkeypatch.setattr(pair_setup, "LOCAL_ENV_PATH", destination)

    pair_setup.write_local_env(
        "02:00:00:00:00:01",
        "hci0",
        False,
    )

    assert destination.read_text() == (
        "BLUEFERRY_MAC=02:00:00:00:00:01\n"
        "BLUEFERRY_ADAPTER=hci0\n"
        "BLUEFERRY_ANCS_ENABLED=false\n"
    )


def test_clear_local_target_preserves_unrelated_preferences(tmp_path, monkeypatch):
    destination = tmp_path / "local.env"
    destination.write_text(
        "BLUEFERRY_MAC=02:00:00:00:00:01\n"
        "BLUEFERRY_ADAPTER=hci7\n"
        "BLUEFERRY_ANCS_ENABLED=false\n"
        "BLUEFERRY_HISTORY_RETENTION_DAYS=14\n"
        "BLUEFERRY_SHOW_NOTIFICATION_CONTENT=false\n"
    )
    monkeypatch.setattr(pair_setup, "LOCAL_ENV_PATH", destination)
    cleared = []
    monkeypatch.setattr(pair_setup, "clear_setup_verification", lambda: cleared.append(True))

    pair_setup.clear_local_target()

    assert destination.read_text() == (
        "BLUEFERRY_HISTORY_RETENTION_DAYS=14\nBLUEFERRY_SHOW_NOTIFICATION_CONTENT=false\n"
    )
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert cleared == [True]


def test_replacing_stale_target_keeps_selected_unpaired_scan_result(monkeypatch):
    device = _device(paired=False)
    calls = []
    monkeypatch.setattr(pair_setup, "_find_device", lambda _mac, **_kwargs: device)
    monkeypatch.setattr(
        pair_setup, "forget_device", lambda _mac, **_kwargs: calls.append("forget")
    )
    monkeypatch.setattr(
        pair_setup, "_stop_user_service", lambda: calls.append("stop")
    )
    monkeypatch.setattr(
        pair_setup, "clear_local_target", lambda: calls.append("clear")
    )

    pair_setup.prepare_target_replacement(device.mac, device.mac)

    assert calls == ["stop", "clear"]
    assert pair_setup._pending_teardown_traces["hci0"] == {
        "reason": "same_unpaired_scan_result_retained",
        "remove_requested": False,
        "before_clear": {"device_present": False},
    }


def test_replacing_a_different_target_removes_its_bluez_device(monkeypatch):
    calls = []
    monkeypatch.setattr(pair_setup, "_find_device", lambda _mac, **_kwargs: None)
    monkeypatch.setattr(
        pair_setup, "forget_device", lambda mac, **_kwargs: calls.append(mac)
    )

    pair_setup.prepare_target_replacement(
        "02:00:00:00:00:01", "02:00:00:00:00:02"
    )

    assert calls == ["02:00:00:00:00:01"]


def test_find_device_stays_on_the_requested_adapter(monkeypatch):
    leftover = _device(paired=True)
    phone = pair_setup.PairedDevice(
        mac=leftover.mac,
        name=leftover.name,
        icon=leftover.icon,
        trusted=True,
        connected=True,
        paired=True,
        adapter_path="/org/bluez/hci1",
        device_path="/org/bluez/hci1/dev_02_00_00_00_00_01",
        uuids=leftover.uuids,
        services_resolved=True,
    )
    monkeypatch.setattr(pair_setup, "list_devices", lambda: [leftover, phone])

    with pytest.raises(pair_setup.PairingError, match="more than one"):
        pair_setup._find_device(leftover.mac)
    assert pair_setup._find_device(leftover.mac, adapter="hci0") == leftover
    assert pair_setup._find_device(leftover.mac, adapter="hci1") == phone
    assert pair_setup._find_device(leftover.mac, adapter="hci2") is None
    assert pair_setup._device(leftover.mac, adapter="hci1") == phone


def test_bluez_device_snapshot_captures_bearers_battery_and_ancs(monkeypatch):
    device = _device(paired=True)
    objects = {
        device.device_path: {
            "org.bluez.Device1": {
                "Paired": True,
                "Trusted": True,
                "Connected": True,
                "ServicesResolved": False,
                "UUIDs": [config.ANCS_SOLICIT_UUID, "0000180f-0000-1000-8000-00805f9b34fb"],
            },
            "org.bluez.Bearer.BREDR1": {"Connected": True},
            "org.bluez.Bearer.LE1": {
                "Paired": True, "Bonded": True, "Connected": True,
            },
        },
        f"{device.device_path}/service0010": {
            "org.bluez.GattService1": {"UUID": config.ANCS_SOLICIT_UUID},
        },
        f"{device.device_path}/service0010/char0011": {
            "org.bluez.GattCharacteristic1": {
                "UUID": "69d1d8f3-45e1-49a8-9821-9bbdfdaad9d9",
            },
        },
        f"{device.device_path}/battery": {
            "org.bluez.Battery1": {"Percentage": 75},
        },
        "/org/bluez/hci0/dev_FF_FF_FF_FF_FF_FF": {
            "org.bluez.Device1": {"Paired": False},
        },
    }

    class Manager:
        @staticmethod
        def GetManagedObjects(*, timeout):
            assert timeout == pair_setup.BLUEZ_SNAPSHOT_TIMEOUT_SECONDS
            return objects

    monkeypatch.setattr(pair_setup, "_object_manager", lambda: Manager())

    snapshot = _BLUEZ_DEVICE_SNAPSHOT(device.device_path)

    assert snapshot["object_present"] is True
    assert snapshot["device"]["ancs_uuid"] is True
    assert snapshot["bearers"]["bredr"] == {
        "present": True, "connected": True,
    }
    assert snapshot["bearers"]["le"] == {
        "present": True, "paired": True, "bonded": True, "connected": True,
    }
    assert snapshot["battery_objects"] == 1
    assert snapshot["gatt"]["services"] == 1
    assert snapshot["gatt"]["characteristics"] == 1
    assert snapshot["gatt"]["ancs_service"] is True
    assert snapshot["gatt"]["ancs_characteristics"] == [
        "69d1d8f3-45e1-49a8-9821-9bbdfdaad9d9",
    ]


def test_forget_records_bluez_state_before_and_after_remove(monkeypatch):
    device = _device(paired=True)
    snapshots = iter([
        {"device_present": True, "battery_objects": 1},
        {"device_present": False, "battery_objects": 0},
    ])

    class Bus:
        @staticmethod
        def get_object(_service, _path):
            return object()

    class Adapter:
        @staticmethod
        def RemoveDevice(_path, *, timeout):
            assert timeout == 30.0

    monkeypatch.setattr(pair_setup, "_find_device", lambda _mac, **_kwargs: device)
    monkeypatch.setattr(pair_setup, "_stop_user_service", lambda: None)
    monkeypatch.setattr(pair_setup, "clear_local_target", lambda: None)
    monkeypatch.setattr(pair_setup, "get_system_bus", Bus)
    monkeypatch.setattr(pair_setup.dbus, "Interface", lambda *_args: Adapter())
    monkeypatch.setattr(pair_setup, "_bluez_device_snapshot", lambda _path: next(snapshots))

    pair_setup.forget_device(device.mac, adapter="hci0")

    trace = pair_setup._pending_teardown_traces["hci0"]
    assert trace["reason"] == "forget_device"
    assert trace["remove_requested"] is True
    assert trace["remove_result"] == "replied"
    assert trace["before_remove"] == {"device_present": True, "battery_objects": 1}
    assert trace["after_remove_reply"] == {
        "device_present": False, "battery_objects": 0,
    }
    assert trace["remove_elapsed_s"] >= 0


def test_bluez_trace_records_only_state_transitions(monkeypatch):
    states = iter([
        {"device_present": True, "device": {"ancs_uuid": False}},
        {"device_present": True, "device": {"ancs_uuid": False}},
        {"device_present": True, "device": {"ancs_uuid": True}},
    ])
    monkeypatch.setattr(pair_setup, "_bluez_device_snapshot", lambda _path: next(states))
    monkeypatch.setattr(pair_setup.time, "monotonic", lambda: 12.5)
    attempt = {"_t0": 10.0}

    pair_setup._record_bluez_state(attempt, "/device", "waiting")
    pair_setup._record_bluez_state(attempt, "/device", "waiting")
    pair_setup._record_bluez_state(attempt, "/device", "waiting")

    assert attempt["bluez_trace"] == [
        {
            "t": 2.5,
            "phase": "waiting",
            "state": {"device_present": True, "device": {"ancs_uuid": False}},
        },
        {
            "t": 2.5,
            "phase": "waiting",
            "state": {"device_present": True, "device": {"ancs_uuid": True}},
        },
    ]


def test_transport_wait_samples_bluez_at_a_bounded_rate(monkeypatch):
    from blueferry.models import BackendStatus

    now = [0.0]
    statuses = [
        BackendStatus(map=True, pbap=True, ancs=False)
        for _index in range(5)
    ] + [BackendStatus(map=True, pbap=True, ancs=True)]
    snapshots = []

    class FakeClient:
        @staticmethod
        def status():
            return statuses.pop(0)

    monkeypatch.setattr("blueferry.client.BackendClient", FakeClient)
    monkeypatch.setattr(pair_setup.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(pair_setup.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    monkeypatch.setattr(
        pair_setup,
        "_record_bluez_state",
        lambda _attempt, _path, _phase: snapshots.append(now[0]),
    )

    result = pair_setup._wait_for_daemon_transports(
        timeout=10,
        attempt={},
        device_path="/device",
    )

    assert result == (True, True, True)
    assert snapshots == [0.0, 2.0]


def test_teardown_trace_survives_quickshell_helper_processes(monkeypatch):
    now = iter([100.0, 105.25])
    monkeypatch.setattr(pair_setup.time, "time", lambda: next(now))
    trace = {
        "reason": "forget_device",
        "after_remove_reply": {"device_present": False},
    }

    pair_setup._save_pending_teardown("hci0", trace)
    pair_setup._pending_teardown_traces.clear()
    restored = pair_setup._take_pending_teardown("hci0")

    assert restored == {
        **trace,
        "capture_age_s": 5.25,
    }
    assert not pair_setup._teardown_trace_path().exists()


def test_forget_stops_backend_removes_bond_and_clears_target(monkeypatch):
    device = _device(paired=True)
    calls = []

    class Bus:
        def get_object(self, _service, _path):
            return object()

    class Adapter:
        def RemoveDevice(self, path, *, timeout):
            calls.append(("remove", str(path), timeout))

    monkeypatch.setattr(pair_setup, "_find_device", lambda _mac, **_kwargs: device)
    monkeypatch.setattr(pair_setup, "_stop_user_service", lambda: calls.append("stop"))
    monkeypatch.setattr(pair_setup, "clear_local_target", lambda: calls.append("clear"))
    monkeypatch.setattr(pair_setup, "get_system_bus", Bus)
    monkeypatch.setattr(pair_setup.dbus, "Interface", lambda *_args: Adapter())

    pair_setup.forget_device(device.mac, adapter="hci0")

    assert calls == [
        "stop",
        ("remove", device.device_path, 30.0),
        "clear",
    ]


def test_forget_removes_the_bond_on_the_saved_adapter(monkeypatch):
    leftover = _device(paired=True)
    phone = pair_setup.PairedDevice(
        mac=leftover.mac,
        name=leftover.name,
        icon=leftover.icon,
        trusted=True,
        connected=True,
        paired=True,
        adapter_path="/org/bluez/hci1",
        device_path="/org/bluez/hci1/dev_02_00_00_00_00_01",
        uuids=leftover.uuids,
        services_resolved=True,
    )
    calls = []

    class Bus:
        def get_object(self, _service, path):
            calls.append(("object", path))
            return object()

    class Adapter:
        def RemoveDevice(self, path, *, timeout):
            calls.append(("remove", str(path), timeout))

    monkeypatch.setattr(pair_setup, "list_devices", lambda: [leftover, phone])
    monkeypatch.setattr(pair_setup, "_stop_user_service", lambda: calls.append("stop"))
    monkeypatch.setattr(pair_setup, "clear_local_target", lambda: calls.append("clear"))
    monkeypatch.setattr(pair_setup, "get_system_bus", Bus)
    monkeypatch.setattr(pair_setup.dbus, "Interface", lambda *_args: Adapter())

    pair_setup.forget_device(leftover.mac, adapter="hci1")

    assert calls == [
        "stop",
        ("object", phone.adapter_path),
        ("remove", phone.device_path, 30.0),
        "clear",
    ]


def test_forget_clears_target_when_another_bluetooth_ui_removed_bond(monkeypatch):
    calls = []
    monkeypatch.setattr(pair_setup, "_find_device", lambda _mac, **_kwargs: None)
    monkeypatch.setattr(pair_setup, "_stop_user_service", lambda: calls.append("stop"))
    monkeypatch.setattr(pair_setup, "clear_local_target", lambda: calls.append("clear"))

    pair_setup.forget_device("02:00:00:00:00:01")

    assert calls == ["stop", "clear"]


def test_forget_treats_bluez_race_as_already_removed(monkeypatch):
    device = _device(paired=True)
    calls = []

    class Bus:
        def get_object(self, _service, _path):
            return object()

    class Adapter:
        def RemoveDevice(self, _path, *, timeout):
            raise pair_setup.dbus.exceptions.DBusException(
                "Already removed",
                name="org.bluez.Error.DoesNotExist",
            )

    monkeypatch.setattr(pair_setup, "_find_device", lambda _mac, **_kwargs: device)
    monkeypatch.setattr(pair_setup, "_stop_user_service", lambda: calls.append("stop"))
    monkeypatch.setattr(pair_setup, "clear_local_target", lambda: calls.append("clear"))
    monkeypatch.setattr(pair_setup, "get_system_bus", Bus)
    monkeypatch.setattr(pair_setup.dbus, "Interface", lambda *_args: Adapter())

    pair_setup.forget_device(device.mac)

    assert calls == ["stop", "clear"]


def test_saved_mac_without_a_bluez_bond_returns_to_first_run(tmp_path, monkeypatch):
    destination = tmp_path / "local.env"
    destination.write_text("BLUEFERRY_MAC=02:00:00:00:00:01\nBLUEFERRY_ADAPTER=hci7\n")
    monkeypatch.setattr(pair_setup, "LOCAL_ENV_PATH", destination)
    monkeypatch.setattr(pair_setup, "bond_status", lambda *_args: False)

    status = pair_setup.configuration_status()

    assert status["saved"] is True
    assert status["bonded"] is False
    assert status["configured"] is False
    assert status["mac"] == "02:00:00:00:00:01"


def test_temporary_bluez_failure_does_not_discard_saved_configuration(
    tmp_path,
    monkeypatch,
):
    destination = tmp_path / "local.env"
    destination.write_text("BLUEFERRY_MAC=02:00:00:00:00:01\n")
    monkeypatch.setattr(pair_setup, "LOCAL_ENV_PATH", destination)
    monkeypatch.setattr(pair_setup, "bond_status", lambda *_args: None)

    status = pair_setup.configuration_status()

    assert status["configured"] is True
    assert status["bonded"] is None


def test_list_devices_includes_unpaired_and_prefers_iphones(monkeypatch):
    class Manager:
        def GetManagedObjects(self):
            return {
                "/org/bluez/hci0/dev_11": {
                    "org.bluez.Device1": {
                        "Address": "00:00:00:00:00:11",
                        "Alias": "Keyboard",
                        "Icon": "input-keyboard",
                        "Paired": True,
                    }
                },
                "/org/bluez/hci0/dev_22": {
                    "org.bluez.Device1": {
                        "Address": "00:00:00:00:00:22",
                        "Name": "iPhone",
                        "Icon": "phone",
                        "Paired": False,
                    }
                },
            }

    monkeypatch.setattr(pair_setup, "_object_manager", lambda: Manager())

    devices = pair_setup.list_devices()

    assert [device.name for device in devices] == ["iPhone", "Keyboard"]
    assert devices[0].paired is False
    assert [device.name for device in pair_setup.list_devices(paired_only=True)] == ["Keyboard"]


def test_headphones_are_not_misclassified_as_an_iphone():
    airpods = _device(paired=True)
    airpods.name = "Test Headphones"
    airpods.icon = "audio-headphones"

    assert airpods.likely_iphone is False


def test_initial_iphone_candidates_exclude_headphones_and_unpaired_devices():
    paired_phone = _device(paired=True)
    headphones = _device(paired=True)
    headphones.name = "Test Headphones"
    headphones.icon = "audio-headphones"
    discovered_phone = _device(paired=False)
    discovered_phone.mac = "02:00:00:00:00:02"

    assert iphone_candidates(
        [
            headphones,
            discovered_phone,
            paired_phone,
        ]
    ) == [paired_phone]


def test_explicit_scan_includes_unpaired_iphones_and_saved_sparse_target():
    discovered_phone = _device(paired=False)
    sparse_target = _device(paired=True)
    sparse_target.mac = "02:00:00:00:00:03"
    sparse_target.name = "Erik's device"
    sparse_target.icon = ""

    assert iphone_candidates(
        [discovered_phone, sparse_target],
        configured_mac=sparse_target.mac,
        include_unpaired=True,
    ) == [discovered_phone, sparse_target]


def test_early_service_resolution_does_not_claim_ancs_is_bonded():
    device = _device(paired=True)
    device.uuids = frozenset()

    assert device.services_resolved is True
    assert device.ancs_bonded is False


def test_bluez_support_status_detects_experimental_running_daemon(
    monkeypatch, tmp_path,
):
    daemon = tmp_path / "812"
    daemon.mkdir()
    daemon.joinpath("cmdline").write_bytes(
        b"/usr/lib/bluetooth/bluetoothd\0-E\0",
    )

    def service_status(argv, **_kwargs):
        stdout = (
            "812\n" if "--property=MainPID" in argv
            else "{ argv[]=/usr/lib/bluetooth/bluetoothd -E ; }\n"
        )
        return type(
            "Result",
            (),
            {
                "returncode": 0,
                "stdout": stdout,
            },
        )()

    monkeypatch.setattr(pair_setup, "run_command", service_status)

    assert pair_setup.bluez_support_status(proc_root=tmp_path)["active"] is True


def test_bluez_support_status_rejects_configured_but_unrestarted_daemon(
    monkeypatch, tmp_path,
):
    daemon = tmp_path / "812"
    daemon.mkdir()
    daemon.joinpath("cmdline").write_bytes(
        b"/usr/lib/bluetooth/bluetoothd\0",
    )

    def service_status(argv, **_kwargs):
        stdout = (
            "812\n" if "--property=MainPID" in argv
            else "{ argv[]=/usr/lib/bluetooth/bluetoothd -E ; }\n"
        )
        return type("Result", (), {"returncode": 0, "stdout": stdout})()

    monkeypatch.setattr(pair_setup, "run_command", service_status)

    assert pair_setup.bluez_support_status(proc_root=tmp_path)["active"] is False


def test_compatibility_is_based_on_controller_features_not_vendor(monkeypatch):
    class Manager:
        def GetManagedObjects(self):
            return {"/org/bluez/hci7": {"org.bluez.Adapter1": {}}}

    monkeypatch.setattr(pair_setup, "_object_manager", lambda: Manager())

    def controller_info(command, **_kwargs):
        if command[0] == "bluetoothctl":
            return type("Result", (), {"returncode": 0, "stdout": "5.87\n"})()
        if command[2] == "0":
            return type("Result", (), {"returncode": 1, "stdout": ""})()
        return type(
            "Result",
            (),
            {
                "returncode": 0,
                "stdout": (
                    "hci7: Primary controller\n"
                    "supported settings: powered ssp br/edr le advertising secure-conn\n"
                    "current settings: powered br/edr le advertising\n"
                ),
            },
        )()

    monkeypatch.setattr(pair_setup, "run_command", controller_info)
    monkeypatch.setattr(
        pair_setup,
        "bluez_support_status",
        lambda: {"active": True},
    )

    status = pair_setup.bluetooth_compatibility()

    assert status["adapter"] == "hci7"
    assert status["hardware_supported"] is True
    assert status["pairing_ready"] is True
    assert status["secure_conn"] is False
    assert "secure-conn" in status["supported_settings"]
    assert status["current_settings"] == [
        "advertising", "br/edr", "le", "powered",
    ]
    assert [item["name"] for item in status["adapters"]] == ["hci7"]


def test_bluez_5_87_without_experimental_api_stays_map_pbap_only(monkeypatch):
    class Manager:
        def GetManagedObjects(self):
            return {"/org/bluez/hci0": {"org.bluez.Adapter1": {}}}

    def controller_info(command, **_kwargs):
        if command[0] == "bluetoothctl":
            return type(
                "Result",
                (),
                {"returncode": 0, "stdout": "bluetoothctl: 5.87\n", "stderr": ""},
            )()
        return type(
            "Result",
            (),
            {
                "returncode": 0,
                "stdout": (
                    "hci0: Primary controller\n"
                    "supported settings: powered ssp br/edr le advertising secure-conn\n"
                    "current settings: powered br/edr le advertising\n"
                ),
            },
        )()

    monkeypatch.setattr(pair_setup, "_object_manager", lambda: Manager())
    monkeypatch.setattr(pair_setup, "run_command", controller_info)
    monkeypatch.setattr(
        pair_setup,
        "bluez_support_status",
        lambda: {"active": False, "packaged_drop_in": False},
    )

    status = pair_setup.bluetooth_compatibility()

    assert status["bluez_version"] == "5.87"
    assert status["messages_supported"] is True
    assert status["notifications_supported"] is False
    assert status["bearer_api_supported"] is False
    assert status["bearer_api_active"] is False
    assert status["pairing_ready"] is True


def test_compatibility_ignores_a_configured_adapter_bluez_does_not_have(
    monkeypatch,
):
    probed = []

    class Manager:
        def GetManagedObjects(self):
            return {"/org/bluez/hci7": {"org.bluez.Adapter1": {}}}

    def controller_info(command, **_kwargs):
        if command[0] == "bluetoothctl":
            return type("Result", (), {"returncode": 0, "stdout": "5.87\n"})()
        index = command[2]
        probed.append(index)
        if index != "7":
            return type("Result", (), {"returncode": 0, "stdout": (
                f"hci{index}: Primary controller\n"
                "supported settings: powered ssp br/edr le advertising secure-conn\n"
            )})()
        return type("Result", (), {"returncode": 0, "stdout": (
            "hci7: Primary controller\n"
            "supported settings: powered ssp br/edr le advertising secure-conn\n"
        )})()

    monkeypatch.setattr(pair_setup, "_object_manager", lambda: Manager())
    monkeypatch.setattr(pair_setup, "run_command", controller_info)
    monkeypatch.setattr(pair_setup, "bluez_support_status", lambda: {"active": True})
    monkeypatch.setattr(pair_setup.config, "ADAPTER", "hci1")

    status = pair_setup.bluetooth_compatibility()

    assert probed == ["7"]
    assert status["adapter"] == "hci7"
    assert [item["name"] for item in status["adapters"]] == ["hci7"]


def test_compatibility_lists_every_adapter_and_honors_an_explicit_choice(monkeypatch):
    monkeypatch.setattr(pair_setup.config, "ADAPTER", "hci0")

    class Manager:
        def GetManagedObjects(self):
            return {
                "/org/bluez/hci0": {"org.bluez.Adapter1": {}},
                "/org/bluez/hci1": {"org.bluez.Adapter1": {}},
            }

    monkeypatch.setattr(pair_setup, "_object_manager", lambda: Manager())

    def controller_info(command, **_kwargs):
        if command[0] == "bluetoothctl":
            return type("Result", (), {"returncode": 0, "stdout": "5.87\n"})()
        index = command[2]
        settings = (
            "powered ssp br/edr le advertising secure-conn"
            if index in {"0", "1"}
            else ""
        )
        return type(
            "Result",
            (),
            {
                "returncode": 0 if settings else 1,
                "stdout": f"hci{index}: Primary controller\nsupported settings: {settings}\n",
            },
        )()

    monkeypatch.setattr(pair_setup, "run_command", controller_info)
    monkeypatch.setattr(pair_setup, "bluez_support_status", lambda: {"active": True})

    automatic = pair_setup.bluetooth_compatibility()
    assert automatic["adapter"] == "hci0"
    assert [item["name"] for item in automatic["adapters"]] == ["hci0", "hci1"]

    selected = pair_setup.bluetooth_compatibility("hci1")
    assert selected["adapter"] == "hci1"
    assert selected["hardware_supported"] is True
    assert [item["name"] for item in selected["adapters"]] == ["hci0", "hci1"]

    monkeypatch.setattr(pair_setup.config, "ADAPTER", "hci1")
    remembered = pair_setup.bluetooth_compatibility()
    assert remembered["adapter"] == "hci1"


def test_compatibility_explains_missing_classic_transport(monkeypatch):
    monkeypatch.setattr(
        pair_setup,
        "_object_manager",
        lambda: (_ for _ in ()).throw(pair_setup.dbus.exceptions.DBusException()),
    )
    monkeypatch.setattr(
        pair_setup,
        "run_command",
        lambda *_args, **_kwargs: type(
            "Result",
            (),
            {
                "returncode": 0,
                "stdout": "supported settings: powered le advertising secure-conn\n",
            },
        )(),
    )
    monkeypatch.setattr(
        pair_setup,
        "bluez_support_status",
        lambda: {"active": True},
    )

    status = pair_setup.bluetooth_compatibility("hci0")

    assert status["hardware_supported"] is False
    assert "BR/EDR" in status["issue"]


def test_classic_only_controller_supports_core_without_ancs(monkeypatch):
    monkeypatch.setattr(
        pair_setup,
        "_object_manager",
        lambda: (_ for _ in ()).throw(pair_setup.dbus.exceptions.DBusException()),
    )
    monkeypatch.setattr(
        pair_setup,
        "run_command",
        lambda *_args, **_kwargs: type(
            "Result",
            (),
            {
                "returncode": 0,
                "stdout": "supported settings: powered ssp br/edr\n",
            },
        )(),
    )
    monkeypatch.setattr(
        pair_setup,
        "bluez_support_status",
        lambda: {"active": False},
    )

    status = pair_setup.bluetooth_compatibility("hci0")

    assert status["messages_supported"] is True
    assert status["notifications_supported"] is False
    assert status["pairing_ready"] is True


def test_controller_snapshot_does_not_repeat_btmgmt_or_systemctl(monkeypatch):
    calls = []

    def run_command(argv, **_kwargs):
        calls.append(list(argv))
        return type(
            "Result",
            (),
            {"returncode": 0, "stdout": "bluetoothctl: 5.87\n", "stderr": ""},
        )()

    monkeypatch.setattr(pair_setup, "run_command", run_command)
    monkeypatch.setattr(pair_setup, "_adapter_dbus_fields", lambda _adapter: {})
    snapshot = pair_setup._controller_snapshot(
        "hci0",
        {
            "available": True,
            "powered": True,
            "classic": True,
            "low_energy": True,
            "advertising": True,
            "secure_pairing": True,
            "secure_conn": True,
            "hardware_supported": True,
            "messages_supported": True,
            "notifications_supported": True,
            "bearer_api_active": True,
            "manufacturer_id": 93,
            "hci_version": 11,
            "supported_settings": ["advertising", "br/edr", "le", "powered", "secure-conn"],
            "current_settings": ["br/edr", "le", "powered", "secure-conn"],
        },
    )

    assert calls == [["bluetoothctl", "--version"]]
    assert snapshot["secure_conn"] is True
    assert snapshot["current_settings"] == ["br/edr", "le", "powered", "secure-conn"]
    assert snapshot["manufacturer_id"] == 93
    assert snapshot["hci_version"] == 11
    assert snapshot["experimental"] is True
    assert snapshot["bluez_version"] == "5.87"
    assert "cod" not in snapshot
    assert "uuids" not in snapshot
    assert "experimental_features" not in snapshot


def test_adapter_dbus_fields_record_cod_uuids_and_experimental_features(
    monkeypatch,
):
    class Properties:
        @staticmethod
        def GetAll(_interface, *, timeout):
            assert timeout == 5.0
            return {
                "Class": 0x7C0408,
                "UUIDs": [
                    "00001133-0000-1000-8000-00805F9B34FB",
                    "0000111E-0000-1000-8000-00805f9b34fb",
                    "00005005-0000-1000-8000-0002ee000001",
                ],
                "ExperimentalFeatures": [
                    "d4992530-b9ec-469f-ab01-6c481c47da1c",
                    "15c0a148-c273-11ea-b3de-0242ac130004",
                    "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                ],
                "Address": "AA:BB:CC:DD:EE:FF",
                "Alias": "erik-laptop",
            }

    class Bus:
        @staticmethod
        def get_object(_service, path):
            assert path == "/org/bluez/hci1"
            return object()

    monkeypatch.setattr(pair_setup, "get_system_bus", Bus)
    monkeypatch.setattr(pair_setup.dbus, "Interface", lambda *_args: Properties())

    fields = pair_setup._adapter_dbus_fields("hci1")

    assert fields["cod"] == "0x7c0408"
    assert fields["cod_handsfree"] is True
    assert fields["uuids"] == [
        "00005005-0000-1000-8000-0002ee000001",
        "handsfree",
        "message-notification-server",
    ]
    assert fields["experimental_features"] == [
        "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "debug",
        "ll-privacy",
    ]
    assert "Address" not in fields
    assert "Alias" not in fields


def test_adapter_dbus_fields_omit_missing_experimental_features(monkeypatch):
    class Properties:
        @staticmethod
        def GetAll(_interface, *, timeout):
            return {"Class": 0x000000, "UUIDs": []}

    monkeypatch.setattr(
        pair_setup, "get_system_bus",
        lambda: type("Bus", (), {"get_object": staticmethod(lambda *_args: object())})(),
    )
    monkeypatch.setattr(pair_setup.dbus, "Interface", lambda *_args: Properties())

    fields = pair_setup._adapter_dbus_fields("hci0")

    assert fields["cod"] == "0x000000"
    assert fields["cod_handsfree"] is False
    assert fields["uuids"] == []
    assert "experimental_features" not in fields


def test_adapter_dbus_fields_are_empty_when_bluez_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        pair_setup,
        "get_system_bus",
        lambda: (_ for _ in ()).throw(pair_setup.dbus.exceptions.DBusException()),
    )

    assert pair_setup._adapter_dbus_fields("hci0") == {}


def test_controller_snapshot_includes_adapter_dbus_fields(monkeypatch):
    monkeypatch.setattr(
        pair_setup,
        "run_command",
        lambda *_args, **_kwargs: type(
            "Result", (), {"returncode": 0, "stdout": "bluetoothctl: 5.87\n", "stderr": ""},
        )(),
    )
    monkeypatch.setattr(
        pair_setup,
        "_adapter_dbus_fields",
        lambda adapter: {
            "cod": "0x240408",
            "cod_handsfree": True,
            "uuids": ["message-notification-server"],
            "experimental_features": ["debug"],
        } if adapter == "hci0" else {},
    )

    snapshot = pair_setup._controller_snapshot(
        "hci0",
        {
            "available": True,
            "powered": True,
            "classic": True,
            "low_energy": True,
            "advertising": True,
            "secure_pairing": True,
            "hardware_supported": True,
            "bearer_api_active": True,
        },
    )

    assert snapshot["cod"] == "0x240408"
    assert snapshot["cod_handsfree"] is True
    assert snapshot["uuids"] == ["message-notification-server"]
    assert snapshot["experimental_features"] == ["debug"]


def test_backend_restart_does_not_create_per_user_enablement(monkeypatch):
    calls = []
    monkeypatch.setattr(
        pair_setup,
        "run_command",
        lambda command, **_kwargs: calls.append(command),
    )

    pair_setup._restart_user_service()

    assert calls == [
        ["/usr/bin/systemctl", "--user", "daemon-reload"],
        ["/usr/bin/systemctl", "--user", "restart", "blueferry.service"],
    ]


def _compatible(monkeypatch, *, notifications: bool = True) -> None:
    monkeypatch.setattr(
        pair_setup,
        "bluetooth_compatibility",
        lambda _adapter: {
            "hardware_supported": True,
            "notifications_supported": notifications,
            "bearer_api_active": True,
            "low_energy": True,
            "advertising": True,
        },
    )
    monkeypatch.setattr(pair_setup, "_prefer_bredr", lambda _path: None)
    monkeypatch.setattr(pair_setup, "_activate_obex_mns", lambda: None)
    monkeypatch.setattr(pair_setup, "_wait_for_classic_settled", lambda _path, **_kwargs: None)
    monkeypatch.setattr(
        pair_setup,
        "_wait_for_daemon_transports",
        lambda **_kwargs: (True, True, True),
    )


def test_complete_pairing_starts_profiles_while_pairing_advert_is_active(monkeypatch):
    device = _device(paired=True)
    calls = []
    monkeypatch.setattr(pair_setup, "_device", lambda _mac, **_kwargs: device)
    _compatible(monkeypatch)
    monkeypatch.setattr(
        bluez_setup,
        "prepare_classic",
        lambda **kwargs: calls.append(("prepare_classic", kwargs["adapter"])) or True,
    )
    monkeypatch.setattr(
        bluez_setup,
        "register_advert",
        lambda adapter, **_kwargs: calls.append(("advert", adapter)) or True,
    )
    monkeypatch.setattr(
        bluez_setup,
        "unregister_advert",
        lambda adapter: calls.append(("unregister", adapter)),
    )
    monkeypatch.setattr(pair_setup, "trust_device", lambda *_args: calls.append("trust"))
    monkeypatch.setattr(
        pair_setup,
        "_prefer_bredr",
        lambda path: calls.append(("prefer-bredr", path)),
    )
    monkeypatch.setattr(pair_setup, "write_local_env", lambda *_args: calls.append("config"))
    monkeypatch.setattr(
        pair_setup,
        "_wait_for_classic_settled",
        lambda path, **_kwargs: calls.append(("classic-settled", path)),
    )
    monkeypatch.setattr(pair_setup, "_restart_user_service", lambda: calls.append("restart"))

    result = pair_setup.complete_pairing(device.mac)

    assert result["ok"] is True
    assert result["ancs"] == "connected"
    assert result["ancs_ready"] is True
    assert calls == [
        ("prepare_classic", "hci0"),
        "trust",
        ("prefer-bredr", device.device_path),
        ("classic-settled", device.device_path),
        ("advert", "hci0"),
        "config",
        "restart",
        ("unregister", "hci0"),
    ]


def test_complete_pairing_prepares_the_selected_adapter_not_a_leftover_bond(
    monkeypatch,
):
    leftover = _device(paired=True)
    phone = pair_setup.PairedDevice(
        mac=leftover.mac,
        name=leftover.name,
        icon=leftover.icon,
        trusted=True,
        connected=False,
        paired=True,
        adapter_path="/org/bluez/hci1",
        device_path="/org/bluez/hci1/dev_02_00_00_00_00_01",
        uuids=leftover.uuids,
        services_resolved=True,
    )
    prepared = []
    saved = []
    monkeypatch.setattr(pair_setup, "list_devices", lambda: [leftover, phone])
    _compatible(monkeypatch)
    monkeypatch.setattr(
        bluez_setup,
        "prepare_classic",
        lambda **kwargs: prepared.append(kwargs["adapter"]) or True,
    )
    monkeypatch.setattr(bluez_setup, "register_advert", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(bluez_setup, "unregister_advert", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pair_setup, "trust_device", lambda *_args: None)
    monkeypatch.setattr(pair_setup, "_prefer_bredr", lambda *_args: None)
    monkeypatch.setattr(pair_setup, "_activate_obex_mns", lambda: None)
    monkeypatch.setattr(pair_setup, "_wait_for_classic_settled", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        pair_setup, "write_local_env",
        lambda mac, adapter=None: saved.append((mac, adapter)),
    )
    monkeypatch.setattr(pair_setup, "_restart_user_service", lambda: None)
    monkeypatch.setattr(
        pair_setup, "_wait_for_daemon_transports",
        lambda **_kwargs: (True, True, True),
    )
    monkeypatch.setattr(pair_setup, "_adapter_dbus_fields", lambda _adapter: {})
    monkeypatch.setattr(pair_setup, "_le_bearer_snapshot", lambda _path: {
        "present": False, "paired": False, "bonded": False, "connected": False,
    })
    monkeypatch.setattr(pair_setup, "_bluetooth_session_owners", lambda: [])

    result = pair_setup.complete_pairing(phone.mac, adapter="hci1")

    assert result["ok"] is True
    assert prepared == ["hci1"]
    assert saved == [(phone.mac, "hci1")]


def test_classic_connect_requires_observable_settled_connection(monkeypatch):
    calls = []

    class Interface:
        def Connect(self, *, reply_handler, **_kwargs):
            calls.append("connect")
            reply_handler()

    class Bus:
        @staticmethod
        def get_object(*_args):
            return object()

    monkeypatch.setattr(pair_setup, "get_system_bus", Bus)
    monkeypatch.setattr(pair_setup.dbus, "Interface", lambda *_args: Interface())
    monkeypatch.setattr(
        pair_setup,
        "_wait_for_classic_settled",
        lambda path, **kwargs: calls.append(("settled", path, kwargs["timeout"])),
    )

    pair_setup._connect_classic(
        "/org/bluez/hci0/dev_02_00_00_00_00_01",
        settle=True,
        timeout=1.0,
    )

    assert calls == [
        "connect",
        ("settled", "/org/bluez/hci0/dev_02_00_00_00_00_01", 1.0),
    ]


def test_post_pair_bearer_preference_is_forced_to_bredr(monkeypatch):
    calls = []

    class Properties:
        @staticmethod
        def Set(interface, name, value):
            calls.append((interface, name, str(value)))

    class Bus:
        @staticmethod
        def get_object(service, path):
            calls.append((service, path))
            return object()

    monkeypatch.setattr(pair_setup, "get_system_bus", Bus)
    monkeypatch.setattr(
        pair_setup.dbus,
        "Interface",
        lambda _obj, interface: (
            calls.append(("interface", interface)) or Properties()
        ),
    )

    pair_setup._prefer_bredr("/org/bluez/hci0/dev_02_00_00_00_00_01")

    assert calls == [
        ("org.bluez", "/org/bluez/hci0/dev_02_00_00_00_00_01"),
        ("interface", "org.freedesktop.DBus.Properties"),
        ("org.bluez.Device1", "PreferredBearer", "bredr"),
    ]


def test_post_pair_bearer_preference_is_optional_when_bluez_omits_it(monkeypatch):
    class Properties:
        @staticmethod
        def Set(_interface, _name, _value):
            raise pair_setup.dbus.exceptions.DBusException(
                "No such property 'PreferredBearer'",
                name="org.bluez.Error.InvalidArguments",
            )

    class Bus:
        @staticmethod
        def get_object(_service, _path):
            return object()

    monkeypatch.setattr(pair_setup, "get_system_bus", Bus)
    monkeypatch.setattr(pair_setup.dbus, "Interface", lambda _obj, _iface: Properties())

    pair_setup._prefer_bredr("/org/bluez/hci0/dev_02_00_00_00_00_01")


def test_post_pair_bearer_preference_still_fails_on_unexpected_errors(monkeypatch):
    class Properties:
        @staticmethod
        def Set(_interface, _name, _value):
            raise pair_setup.dbus.exceptions.DBusException(
                "Permission denied",
                name="org.freedesktop.DBus.Error.AccessDenied",
            )

    class Bus:
        @staticmethod
        def get_object(_service, _path):
            return object()

    monkeypatch.setattr(pair_setup, "get_system_bus", Bus)
    monkeypatch.setattr(pair_setup.dbus, "Interface", lambda _obj, _iface: Properties())

    with pytest.raises(pair_setup.PairingError, match="Permission denied"):
        pair_setup._prefer_bredr("/org/bluez/hci0/dev_02_00_00_00_00_01")


def test_obex_mns_is_activated_before_pairing(monkeypatch):
    calls = []

    class Peer:
        @staticmethod
        def Ping(*, timeout):
            calls.append(("ping", timeout))

    class Bus:
        @staticmethod
        def get_object(service, path):
            calls.append((service, path))
            return object()

    monkeypatch.setattr(pair_setup, "get_session_bus", Bus)
    monkeypatch.setattr(
        pair_setup.dbus,
        "Interface",
        lambda _object, interface: calls.append(("interface", interface)) or Peer(),
    )

    pair_setup._activate_obex_mns()

    assert calls == [
        ("org.bluez.obex", "/org/bluez/obex"),
        ("interface", "org.freedesktop.DBus.Peer"),
        ("ping", 10.0),
    ]


def test_ancs_retries_local_abort_after_classic_resettles(monkeypatch, caplog):
    device = _device(paired=True)
    connects = []
    settled = []
    caplog.set_level(logging.DEBUG, logger="blueferry.pair_setup")

    class Manager:
        @staticmethod
        def GetManagedObjects():
            return {
                pair_setup.dbus.ObjectPath(device.device_path): {
                    "org.bluez.Bearer.LE1": {},
                }
            }

    class Properties:
        @staticmethod
        def Set(*_args, **_kwargs):
            return None

    class LeBearer:
        @staticmethod
        def Connect(**_kwargs):
            connects.append(True)
            if len(connects) == 1:
                raise pair_setup.dbus.exceptions.DBusException(
                    "le-connection-abort-by-local",
                    name="org.bluez.Error.Failed",
                )

    monkeypatch.setattr(pair_setup, "_object_manager", lambda: Manager())
    monkeypatch.setattr(
        pair_setup,
        "_wait_for_classic_settled",
        lambda path, **_kwargs: settled.append(path),
    )
    monkeypatch.setattr(
        pair_setup,
        "get_system_bus",
        lambda: type("Bus", (), {"get_object": staticmethod(lambda *_args: object())})(),
    )
    monkeypatch.setattr(
        pair_setup.dbus,
        "Interface",
        lambda _obj, interface: (
            Properties()
            if interface == "org.freedesktop.DBus.Properties"
            else LeBearer()
        ),
    )

    assert pair_setup._connect_ancs(device) == "connected"
    assert len(connects) == 2
    assert settled == [device.device_path]
    assert "LE bearer probe: paired=False bonded=False connected=False" in caplog.text
    assert "sending Bearer.LE1.Connect (attempt 1/2)" in caplog.text
    assert "le-connection-abort-by-local" in caplog.text
    assert "sending Bearer.LE1.Connect (attempt 2/2)" in caplog.text
    assert "LE bearer connected" in caplog.text


def test_connect_ancs_continues_when_preferred_bearer_is_missing(monkeypatch):
    device = _device(paired=True)
    connects = []

    class Manager:
        @staticmethod
        def GetManagedObjects():
            return {
                pair_setup.dbus.ObjectPath(device.device_path): {
                    "org.bluez.Bearer.LE1": {},
                }
            }

    class Properties:
        @staticmethod
        def Set(*_args, **_kwargs):
            raise pair_setup.dbus.exceptions.DBusException(
                "No such property 'PreferredBearer'",
                name="org.bluez.Error.InvalidArguments",
            )

    class LeBearer:
        @staticmethod
        def Connect(**_kwargs):
            connects.append(True)

    monkeypatch.setattr(pair_setup, "_object_manager", lambda: Manager())
    monkeypatch.setattr(
        pair_setup,
        "get_system_bus",
        lambda: type("Bus", (), {"get_object": staticmethod(lambda *_args: object())})(),
    )
    monkeypatch.setattr(
        pair_setup.dbus,
        "Interface",
        lambda _obj, interface: (
            Properties()
            if interface == "org.freedesktop.DBus.Properties"
            else LeBearer()
        ),
    )

    assert pair_setup._connect_ancs(device) == "connected"
    assert connects == [True]


def test_complete_pairing_headless_pairs_from_linux(monkeypatch):
    unpaired = _device(paired=False)
    paired = _device(paired=True)
    calls = []

    class DeviceInterface:
        def Pair(self, **kwargs):
            calls.append(("pair", kwargs["timeout"]))

    class Bus:
        @staticmethod
        def get_object(*_args):
            return object()

    monkeypatch.setattr(pair_setup, "_device", lambda _mac, **_kwargs: unpaired)
    monkeypatch.setattr(pair_setup, "_wait_for_paired_device", lambda _mac, **_kwargs: paired)
    _compatible(monkeypatch)
    monkeypatch.setattr(bluez_setup, "prepare_classic", lambda **_kwargs: True)
    monkeypatch.setattr(bluez_setup, "register_advert", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(bluez_setup, "unregister_advert", lambda _adapter: None)
    monkeypatch.setattr(pair_setup, "get_system_bus", Bus)
    monkeypatch.setattr(pair_setup.dbus, "Interface", lambda *_args: DeviceInterface())
    monkeypatch.setattr(pair_setup, "trust_device", lambda *_args: None)
    monkeypatch.setattr(pair_setup, "write_local_env", lambda *_args: None)
    monkeypatch.setattr(pair_setup, "_restart_user_service", lambda: None)

    result = pair_setup.complete_pairing(unpaired.mac)

    assert calls == [("pair", 120.0)]
    assert result["ok"] is True
    assert result["ancs"] == "connected"


def test_compatibility_pairing_connects_and_lets_the_iphone_initiate(monkeypatch):
    from blueferry import pairing_agent

    unpaired = _device(paired=False)
    paired = _device(paired=True)
    calls = []

    class Agent:
        def __init__(self, path, confirmation, display, *, make_default):
            calls.append(("agent", path, confirmation, display, make_default))

        def __enter__(self):
            calls.append("agent-enter")
            return self

        def __exit__(self, *_args):
            calls.append("agent-exit")

        def wait_for_pair(self, *, timeout):
            calls.append(("wait", timeout))

        def pair(self, *, timeout):
            pytest.fail(f"compatibility mode must not call Pair ({timeout})")

    def confirmation(_passkey):
        return True

    def display(_passkey):
        return None

    monkeypatch.setattr(pair_setup, "_device", lambda _mac, **_kwargs: unpaired)
    monkeypatch.setattr(
        pair_setup,
        "_wait_for_paired_device",
        lambda _mac, **_kwargs: calls.append("settled") or paired,
    )
    _compatible(monkeypatch)
    monkeypatch.setattr(bluez_setup, "prepare_classic", lambda **_kwargs: True)
    monkeypatch.setattr(bluez_setup, "register_advert", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        bluez_setup,
        "unregister_advert",
        lambda _adapter: calls.append("advert-exit"),
    )
    monkeypatch.setattr(pairing_agent, "RegisteredPairingAgent", Agent)

    def connect_classic(path, **kwargs):
        calls.append(("connect", path, kwargs["timeout"]))

    monkeypatch.setattr(pair_setup, "_connect_classic", connect_classic)
    monkeypatch.setattr(
        pair_setup,
        "_wait_for_classic_settled",
        lambda path, **_kwargs: calls.append(("classic-settled", path)),
    )
    monkeypatch.setattr(
        pair_setup,
        "trust_device",
        lambda *_args: calls.append("trust"),
    )
    monkeypatch.setattr(
        pair_setup,
        "_prefer_bredr",
        lambda path: calls.append(("prefer-bredr", path)),
    )
    monkeypatch.setattr(
        pair_setup,
        "write_local_env",
        lambda *_args: calls.append("persist"),
    )
    monkeypatch.setattr(
        pair_setup,
        "_restart_user_service",
        lambda: calls.append("daemon"),
    )

    pair_setup.complete_pairing(
        unpaired.mac,
        confirmation=confirmation,
        display=display,
        compatibility_mode=True,
    )

    # Compatibility mode uses Connect so iOS initiates authentication; there
    # must be no competing Linux-side Device1.Pair() call. After the Classic
    # bond is trusted and settled, the solicitation is advertised before the
    # daemon's first MAP/PBAP attempt, while ANCS remains disabled.
    assert calls[0][0] == "agent"
    assert calls[0][1] == unpaired.device_path
    assert calls[0][2] is not confirmation
    assert calls[0][2](12) is True
    assert calls[0][4] is True
    assert calls[1:] == [
        "agent-enter",
        ("connect", unpaired.device_path, 60.0),
        ("wait", 120.0),
        "settled",
        "trust",
        ("prefer-bredr", paired.device_path),
        ("classic-settled", paired.device_path),
        "persist",
        "daemon",
        "advert-exit",
        "agent-exit",
    ]


def test_default_interactive_pairing_lets_the_iphone_initiate(monkeypatch):
    from blueferry import pairing_agent

    unpaired = _device(paired=False)
    paired = _device(paired=True)
    calls = []

    class Agent:
        def __init__(self, path, confirmation, display, *, make_default):
            calls.append(("agent", path, make_default))

        def __enter__(self):
            calls.append("agent-enter")
            return self

        def __exit__(self, *_args):
            calls.append("agent-exit")

        def pair(self, *, timeout):
            pytest.fail(f"successful Connect must not call Pair ({timeout})")

        def wait_for_pair(self, *, timeout):
            calls.append(("wait", timeout))

    monkeypatch.setattr(pair_setup, "_device", lambda _mac, **_kwargs: unpaired)
    monkeypatch.setattr(
        pair_setup,
        "_wait_for_paired_device",
        lambda _mac, **_kwargs: calls.append("settled") or paired,
    )
    _compatible(monkeypatch)
    monkeypatch.setattr(bluez_setup, "prepare_classic", lambda **_kwargs: True)
    monkeypatch.setattr(bluez_setup, "register_advert", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        bluez_setup,
        "unregister_advert",
        lambda _adapter: calls.append("advert-exit"),
    )
    monkeypatch.setattr(pairing_agent, "RegisteredPairingAgent", Agent)
    monkeypatch.setattr(
        pair_setup,
        "_connect_classic",
        lambda path, **kwargs: calls.append(("connect", path, kwargs["timeout"])),
    )
    monkeypatch.setattr(
        pair_setup,
        "_wait_for_classic_settled",
        lambda path, **_kwargs: calls.append(("classic-settled", path)),
    )
    monkeypatch.setattr(pair_setup, "trust_device", lambda *_args: calls.append("trust"))
    monkeypatch.setattr(
        pair_setup,
        "_prefer_bredr",
        lambda path: calls.append(("prefer-bredr", path)),
    )
    monkeypatch.setattr(pair_setup, "write_local_env", lambda *_args: calls.append("persist"))
    monkeypatch.setattr(pair_setup, "_restart_user_service", lambda: calls.append("daemon"))

    pair_setup.complete_pairing(
        unpaired.mac,
        confirmation=lambda _passkey: True,
    )

    assert calls == [
        ("agent", unpaired.device_path, True),
        "agent-enter",
        ("connect", unpaired.device_path, 60.0),
        ("wait", 120.0),
        "settled",
        "trust",
        ("prefer-bredr", paired.device_path),
        ("classic-settled", paired.device_path),
        "persist",
        "daemon",
        "advert-exit",
        "agent-exit",
    ]


def test_interactive_pairing_can_use_explicit_pair_without_connecting(monkeypatch):
    from blueferry import pairing_agent

    unpaired = _device(paired=False)
    paired = _device(paired=True)
    calls = []

    class Agent:
        def __init__(self, path, confirmation, display, *, make_default):
            calls.append(("agent", path, make_default))

        def __enter__(self):
            calls.append("agent-enter")
            return self

        def __exit__(self, *_args):
            calls.append("agent-exit")

        def pair(self, *, timeout):
            calls.append(("pair", timeout))

        def wait_for_pair(self, *, timeout):
            pytest.fail(f"explicit pairing must not wait for peer pairing ({timeout})")

    device_results = iter([unpaired])
    monkeypatch.setattr(
        pair_setup,
        "_device",
        lambda _mac, **_kwargs: next(device_results, paired),
    )
    monkeypatch.setattr(
        pair_setup,
        "_wait_for_paired_device",
        lambda _mac, **_kwargs: calls.append("settled") or paired,
    )
    _compatible(monkeypatch)
    monkeypatch.setattr(bluez_setup, "prepare_classic", lambda **_kwargs: True)
    monkeypatch.setattr(bluez_setup, "register_advert", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        bluez_setup,
        "unregister_advert",
        lambda _adapter: calls.append("advert-exit"),
    )
    monkeypatch.setattr(pairing_agent, "RegisteredPairingAgent", Agent)
    monkeypatch.setattr(
        pair_setup,
        "_connect_classic",
        lambda *_args, **_kwargs: pytest.fail(
            "explicit pairing must not call Connect"
        ),
    )
    monkeypatch.setattr(pair_setup, "trust_device", lambda *_args: calls.append("trust"))
    monkeypatch.setattr(pair_setup, "write_local_env", lambda *_args: calls.append("persist"))
    monkeypatch.setattr(pair_setup, "_restart_user_service", lambda: calls.append("daemon"))

    result = pair_setup.complete_pairing(
        unpaired.mac,
        confirmation=lambda _passkey: True,
        explicit_pairing=True,
    )

    assert result["ok"] is True
    assert calls[:5] == [
        ("agent", unpaired.device_path, False),
        "agent-enter",
        ("pair", 120.0),
        "settled",
        "trust",
    ]
    report = pair_setup.quirks_report.issue_report()
    assert report is not None
    payload = report.read_text()
    assert '"pairing_transaction": "explicit-device-pair"' in payload
    assert '"event": "pair_fallback"' not in payload


def test_peer_initiated_pairing_is_allowed_to_finish(monkeypatch):
    unpaired = _device(paired=False)
    paired = _device(paired=True)
    pair_calls = []

    class DeviceInterface:
        def Pair(self, **_kwargs):
            pair_calls.append(True)
            raise pair_setup.dbus.exceptions.DBusException(
                "Pairing already in progress",
                name="org.bluez.Error.InProgress",
            )

    class Bus:
        @staticmethod
        def get_object(*_args):
            return object()

    monkeypatch.setattr(pair_setup, "_device", lambda _mac, **_kwargs: unpaired)
    monkeypatch.setattr(pair_setup, "_wait_for_paired_device", lambda _mac, **_kwargs: paired)
    _compatible(monkeypatch)
    monkeypatch.setattr(bluez_setup, "prepare_classic", lambda **_kwargs: True)
    monkeypatch.setattr(bluez_setup, "register_advert", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(bluez_setup, "unregister_advert", lambda _adapter: None)
    monkeypatch.setattr(pair_setup, "get_system_bus", Bus)
    monkeypatch.setattr(pair_setup.dbus, "Interface", lambda *_args: DeviceInterface())
    monkeypatch.setattr(pair_setup, "trust_device", lambda *_args: None)
    monkeypatch.setattr(pair_setup, "write_local_env", lambda *_args: None)
    monkeypatch.setattr(pair_setup, "_restart_user_service", lambda: None)

    result = pair_setup.complete_pairing(unpaired.mac)

    assert pair_calls == [True]
    assert result["ok"] is True


def test_pairing_starts_daemon_even_when_ancs_is_still_missing(monkeypatch):
    device = _device(paired=True)
    device.uuids = frozenset()
    monkeypatch.setattr(pair_setup, "_device", lambda _mac, **_kwargs: device)
    _compatible(monkeypatch)
    monkeypatch.setattr(bluez_setup, "prepare_classic", lambda **_kwargs: True)
    monkeypatch.setattr(bluez_setup, "register_advert", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(bluez_setup, "unregister_advert", lambda _adapter: None)
    monkeypatch.setattr(pair_setup, "trust_device", lambda *_args: None)
    saved = []
    restarted = []
    monkeypatch.setattr(pair_setup, "write_local_env", lambda *_args: saved.append(True))
    monkeypatch.setattr(
        pair_setup,
        "_wait_for_daemon_transports",
        lambda **_kwargs: (True, True, False),
    )
    monkeypatch.setattr(pair_setup, "_restart_user_service", lambda: restarted.append(True))

    result = pair_setup.complete_pairing(device.mac)

    assert saved == [True]
    assert restarted == [True]
    assert result["ancs"] == "daemon connecting"
    assert result["ancs_ready"] is False


def test_pairing_does_not_gate_map_on_inbound_ancs(monkeypatch):
    device = _device(paired=True)
    monkeypatch.setattr(pair_setup, "_device", lambda _mac, **_kwargs: device)
    _compatible(monkeypatch)
    monkeypatch.setattr(bluez_setup, "prepare_classic", lambda **_kwargs: True)
    monkeypatch.setattr(bluez_setup, "register_advert", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(bluez_setup, "unregister_advert", lambda _adapter: None)
    monkeypatch.setattr(pair_setup, "trust_device", lambda *_args: None)
    monkeypatch.setattr(
        pair_setup,
        "_wait_for_daemon_transports",
        lambda **_kwargs: (False, False, False),
    )
    saved = []
    monkeypatch.setattr(pair_setup, "write_local_env", lambda *_args: saved.append(True))
    monkeypatch.setattr(pair_setup, "_restart_user_service", lambda: None)

    result = pair_setup.complete_pairing(device.mac)

    assert saved == [True]
    assert result["ancs_ready"] is False


def test_pairing_without_bearer_api_continues_with_map_and_pbap(monkeypatch):
    device = _device(paired=True)
    monkeypatch.setattr(pair_setup, "_device", lambda _mac, **_kwargs: device)
    _compatible(monkeypatch, notifications=False)
    monkeypatch.setattr(bluez_setup, "prepare_classic", lambda **_kwargs: True)
    adverts = []
    monkeypatch.setattr(
        bluez_setup,
        "register_advert",
        lambda *_args, **_kwargs: adverts.append("registered") or True,
    )
    monkeypatch.setattr(
        bluez_setup,
        "unregister_advert",
        lambda *_args: adverts.append("removed"),
    )
    monkeypatch.setattr(pair_setup, "trust_device", lambda *_args: None)
    monkeypatch.setattr(pair_setup, "_prefer_bredr", lambda *_args: None)
    monkeypatch.setattr(
        pair_setup,
        "_wait_for_daemon_transports",
        lambda **kwargs: (
            pytest.fail("MAP/PBAP-only flag was not passed")
            if kwargs.get("notifications_supported") is not False
            else (True, True, False)
        ),
    )
    saved = []
    monkeypatch.setattr(pair_setup, "write_local_env", lambda *_args: saved.append(True))
    monkeypatch.setattr(pair_setup, "_restart_user_service", lambda: None)

    result = pair_setup.complete_pairing(device.mac)

    assert saved == [True]
    assert adverts == ["registered", "removed"]
    assert result["ancs"] == "disabled"
    assert result["ancs_enabled"] is False
    assert result["ancs_ready"] is False
