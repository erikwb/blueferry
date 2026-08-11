"""Shared graphical pairing workflow regressions."""

from __future__ import annotations

import logging
import stat

import pytest

from blueferry import bluez_setup, config, pair_setup
from blueferry.bluetooth_devices import iphone_candidates


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
        "BLUEFERRY_SHOW_NOTIFICATION_CONTENT=false\n"
    )
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


def test_write_local_env_rejects_environment_injection(tmp_path, monkeypatch):
    monkeypatch.setattr(pair_setup, "LOCAL_ENV_PATH", tmp_path / "local.env")

    with pytest.raises(pair_setup.PairingError):
        pair_setup.write_local_env("02:00:00:00:00:01\nEVIL=1", "hci0")
    with pytest.raises(pair_setup.PairingError):
        pair_setup.write_local_env("02:00:00:00:00:01", "hci0\nEVIL=1")


def test_clear_local_target_preserves_unrelated_preferences(tmp_path, monkeypatch):
    destination = tmp_path / "local.env"
    destination.write_text(
        "BLUEFERRY_MAC=02:00:00:00:00:01\n"
        "BLUEFERRY_ADAPTER=hci7\n"
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


def test_forget_stops_backend_removes_bond_and_clears_target(monkeypatch):
    device = _device(paired=True)
    calls = []

    class Bus:
        def get_object(self, _service, _path):
            return object()

    class Adapter:
        def RemoveDevice(self, path, *, timeout):
            calls.append(("remove", str(path), timeout))

    monkeypatch.setattr(pair_setup, "_find_device", lambda _mac: device)
    monkeypatch.setattr(pair_setup, "_stop_user_service", lambda: calls.append("stop"))
    monkeypatch.setattr(pair_setup, "clear_local_target", lambda: calls.append("clear"))
    monkeypatch.setattr(pair_setup, "get_system_bus", Bus)
    monkeypatch.setattr(pair_setup.dbus, "Interface", lambda *_args: Adapter())

    pair_setup.forget_device(device.mac)

    assert calls == [
        "stop",
        ("remove", device.device_path, 30.0),
        "clear",
    ]


def test_forget_clears_target_when_another_bluetooth_ui_removed_bond(monkeypatch):
    calls = []
    monkeypatch.setattr(pair_setup, "_find_device", lambda _mac: None)
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

    monkeypatch.setattr(pair_setup, "_find_device", lambda _mac: device)
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


def test_bluez_support_status_detects_experimental_execstart(monkeypatch):
    monkeypatch.setattr(
        pair_setup,
        "run_command",
        lambda *_args, **_kwargs: type(
            "Result",
            (),
            {
                "returncode": 0,
                "stdout": "{ argv[]=/usr/lib/bluetooth/bluetoothd -E ; }\n",
            },
        )(),
    )

    assert pair_setup.bluez_support_status()["active"] is True


def test_compatibility_is_based_on_controller_features_not_vendor(monkeypatch):
    class Manager:
        def GetManagedObjects(self):
            return {"/org/bluez/hci7": {"org.bluez.Adapter1": {}}}

    monkeypatch.setattr(pair_setup, "_object_manager", lambda: Manager())

    def controller_info(command, **_kwargs):
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
        },
    )


def test_complete_pairing_advertises_only_after_bond_then_hands_to_daemon(monkeypatch):
    device = _device(paired=True)
    calls = []
    monkeypatch.setattr(pair_setup, "_device", lambda _mac: device)
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
    monkeypatch.setattr(pair_setup, "write_local_env", lambda *_args: calls.append("config"))
    monkeypatch.setattr(
        pair_setup, "_connect_classic", lambda _path: calls.append("classic-connect")
    )
    monkeypatch.setattr(
        pair_setup, "_connect_ancs", lambda _device: calls.append("ancs") or "connected"
    )
    monkeypatch.setattr(pair_setup, "_restart_user_service", lambda: calls.append("restart"))

    result = pair_setup.complete_pairing(device.mac)

    assert result["ok"] is True
    assert result["ancs"] == "connected"
    assert result["ancs_ready"] is True
    assert calls == [
        ("prepare_classic", "hci0"),
        "trust",
        "classic-connect",
        ("advert", "hci0"),
        "ancs",
        ("unregister", "hci0"),
        "config",
        "restart",
    ]


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

    monkeypatch.setattr(pair_setup, "_device", lambda _mac: unpaired)
    monkeypatch.setattr(pair_setup, "_wait_for_paired_device", lambda _mac: paired)
    _compatible(monkeypatch)
    monkeypatch.setattr(bluez_setup, "prepare_classic", lambda **_kwargs: True)
    monkeypatch.setattr(bluez_setup, "register_advert", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(bluez_setup, "unregister_advert", lambda _adapter: None)
    monkeypatch.setattr(pair_setup, "get_system_bus", Bus)
    monkeypatch.setattr(pair_setup.dbus, "Interface", lambda *_args: DeviceInterface())
    monkeypatch.setattr(pair_setup, "trust_device", lambda *_args: None)
    monkeypatch.setattr(pair_setup, "_connect_classic", lambda _path: None)
    monkeypatch.setattr(pair_setup, "_connect_ancs", lambda _device: "connected")
    monkeypatch.setattr(pair_setup, "write_local_env", lambda *_args: None)
    monkeypatch.setattr(pair_setup, "_restart_user_service", lambda: None)

    result = pair_setup.complete_pairing(unpaired.mac)

    assert calls == [("pair", 120.0)]
    assert result["ok"] is True
    assert result["ancs"] == "connected"


def test_interactive_pairing_connects_and_lets_the_iphone_initiate(monkeypatch):
    from blueferry import pairing_agent

    unpaired = _device(paired=False)
    paired = _device(paired=True)
    calls = []

    class Agent:
        def __init__(self, path, confirmation, display):
            calls.append(("agent", path, confirmation, display))

        def __enter__(self):
            calls.append("agent-enter")
            return self

        def __exit__(self, *_args):
            calls.append("agent-exit")

        def wait_for_pair(self, *, timeout):
            calls.append(("wait", timeout))

    def confirmation(_passkey):
        return True

    def display(_passkey):
        return None

    monkeypatch.setattr(pair_setup, "_device", lambda _mac: unpaired)
    monkeypatch.setattr(
        pair_setup,
        "_wait_for_paired_device",
        lambda _mac: calls.append("settled") or paired,
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
        if "timeout" in kwargs:
            calls.append(("connect", path, kwargs["timeout"]))
        else:
            calls.append(("post-pair-connect", path))

    monkeypatch.setattr(pair_setup, "_connect_classic", connect_classic)
    monkeypatch.setattr(
        pair_setup,
        "_connect_ancs",
        lambda _device: calls.append("ancs") or "connected",
    )
    monkeypatch.setattr(
        pair_setup,
        "trust_device",
        lambda *_args: calls.append("trust"),
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
    )

    # Connecting the unpaired ACL makes iOS initiate pairing (and derive the
    # LE keys); there must be no competing Linux-side Device1.Pair() call.
    # Keep our agent default until the Classic-to-LE handoff finishes so a
    # restored desktop agent cannot race the remainder of the transaction.
    assert calls == [
        ("agent", unpaired.device_path, confirmation, display),
        "agent-enter",
        ("connect", unpaired.device_path, 60.0),
        ("wait", 120.0),
        "settled",
        "trust",
        ("post-pair-connect", unpaired.device_path),
        "ancs",
        "advert-exit",
        "agent-exit",
        "persist",
        "daemon",
    ]


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

    monkeypatch.setattr(pair_setup, "_device", lambda _mac: unpaired)
    monkeypatch.setattr(pair_setup, "_wait_for_paired_device", lambda _mac: paired)
    _compatible(monkeypatch)
    monkeypatch.setattr(bluez_setup, "prepare_classic", lambda **_kwargs: True)
    monkeypatch.setattr(bluez_setup, "register_advert", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(bluez_setup, "unregister_advert", lambda _adapter: None)
    monkeypatch.setattr(pair_setup, "get_system_bus", Bus)
    monkeypatch.setattr(pair_setup.dbus, "Interface", lambda *_args: DeviceInterface())
    monkeypatch.setattr(pair_setup, "trust_device", lambda *_args: None)
    monkeypatch.setattr(pair_setup, "_connect_classic", lambda _path: None)
    monkeypatch.setattr(pair_setup, "_connect_ancs", lambda _device: "connected")
    monkeypatch.setattr(pair_setup, "write_local_env", lambda *_args: None)
    monkeypatch.setattr(pair_setup, "_restart_user_service", lambda: None)

    result = pair_setup.complete_pairing(unpaired.mac)

    assert pair_calls == [True]
    assert result["ok"] is True


def test_pairing_does_not_save_or_start_daemon_when_ancs_is_missing(monkeypatch):
    device = _device(paired=True)
    device.uuids = frozenset()
    monkeypatch.setattr(pair_setup, "_device", lambda _mac: device)
    _compatible(monkeypatch)
    monkeypatch.setattr(bluez_setup, "prepare_classic", lambda **_kwargs: True)
    monkeypatch.setattr(bluez_setup, "register_advert", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(bluez_setup, "unregister_advert", lambda _adapter: None)
    monkeypatch.setattr(pair_setup, "trust_device", lambda *_args: None)
    saved = []
    restarted = []
    monkeypatch.setattr(pair_setup, "write_local_env", lambda *_args: saved.append(True))
    monkeypatch.setattr(pair_setup, "_connect_classic", lambda _path: None)

    def no_le(_device):
        raise pair_setup.PairingError("LE bond missing")

    monkeypatch.setattr(pair_setup, "_connect_ancs", no_le)
    monkeypatch.setattr(pair_setup, "_restart_user_service", lambda: restarted.append(True))

    with pytest.raises(pair_setup.PairingError, match="iPhone was not saved"):
        pair_setup.complete_pairing(device.mac)

    assert saved == []
    assert restarted == []


def test_pairing_does_not_save_while_waiting_for_inbound_ancs(monkeypatch):
    device = _device(paired=True)
    monkeypatch.setattr(pair_setup, "_device", lambda _mac: device)
    _compatible(monkeypatch)
    monkeypatch.setattr(bluez_setup, "prepare_classic", lambda **_kwargs: True)
    monkeypatch.setattr(bluez_setup, "register_advert", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(bluez_setup, "unregister_advert", lambda _adapter: None)
    monkeypatch.setattr(pair_setup, "trust_device", lambda *_args: None)
    monkeypatch.setattr(pair_setup, "_connect_classic", lambda _path: None)
    monkeypatch.setattr(
        pair_setup,
        "_connect_ancs",
        lambda _device: "waiting for iPhone to connect",
    )
    saved = []
    monkeypatch.setattr(pair_setup, "write_local_env", lambda *_args: saved.append(True))

    with pytest.raises(pair_setup.PairingError, match="iPhone was not saved"):
        pair_setup.complete_pairing(device.mac)

    assert saved == []


def test_pairing_rejects_controller_without_ancs_before_saving_target(monkeypatch):
    device = _device(paired=True)
    monkeypatch.setattr(pair_setup, "_device", lambda _mac: device)
    _compatible(monkeypatch, notifications=False)
    saved = []
    monkeypatch.setattr(pair_setup, "write_local_env", lambda *_args: saved.append(True))

    with pytest.raises(pair_setup.PairingError, match="successful ANCS/LE connection"):
        pair_setup.complete_pairing(device.mac)

    assert saved == []
