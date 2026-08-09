"""BLE advertisement shape and cleanup regressions."""
from __future__ import annotations

import dbus
import pytest

from blueferry import bluez_setup, config


class TestAncsAdvertisement:
    def test_pairing_payload_is_discoverable_and_solicits_ancs(self):
        props = bluez_setup._AncsAdvert.GetAll(
            None, "org.bluez.LEAdvertisement1"
        )

        assert str(props["Type"]) == "peripheral"
        assert list(props["SolicitUUIDs"]) == [config.ANCS_SOLICIT_UUID]
        assert bool(props["Discoverable"])
        assert int(props["DiscoverableTimeout"]) == 180
        assert props["ManufacturerData"].signature == "qv"
        assert props["ServiceData"].signature == "sv"
        assert bytes(props["ManufacturerData"][dbus.UInt16(0xFFFF)]) == (
            b"\x50\xb0\x13\xf0"
        )
        assert bytes(props["ServiceData"][
            "00009999-0000-1000-8000-00805f9b34fb"
        ]) == b"\x9e\x85\x39\x96"

    def test_rejects_unknown_interface(self):
        with pytest.raises(dbus.exceptions.DBusException):
            bluez_setup._AncsAdvert.GetAll(None, "not.the.advert.interface")


def test_daemon_run_cleans_up_when_start_raises(monkeypatch):
    """A partial startup must not leak a hardware advertisement."""
    from blueferry import daemon as daemon_mod

    instance = object.__new__(daemon_mod.Daemon)
    stopped = []

    def fail_start():
        raise RuntimeError("partial startup")

    monkeypatch.setattr(instance, "start", fail_start)
    monkeypatch.setattr(instance, "stop", lambda: stopped.append(True))

    with pytest.raises(RuntimeError, match="partial startup"):
        instance.run()

    assert stopped == [True]


def test_cod_change_requires_explicit_authorization(monkeypatch):
    calls = []
    monkeypatch.setattr(bluez_setup.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(
        bluez_setup,
        "run_command",
        lambda args, **_kwargs: calls.append(args),
    )

    assert bluez_setup.set_cod(authorize=False) is False
    assert calls == []


def test_authorized_cod_change_uses_polkit_and_fixed_command(monkeypatch):
    calls = []
    monkeypatch.setattr(bluez_setup.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(bluez_setup.os.path, "isfile", lambda _path: True)
    monkeypatch.setattr(bluez_setup.os, "access", lambda _path, _mode: True)
    monkeypatch.setattr(
        bluez_setup,
        "run_command",
        lambda args, **kwargs: calls.append((args, kwargs))
        or type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
    )

    assert bluez_setup.set_cod(adapter="hci7", authorize=True) is True
    assert calls[0][0] == [
        "/usr/bin/pkexec", "/usr/bin/btmgmt", "--index", "7", "class", "4", "8",
    ]
    assert calls[0][1]["timeout"] == 120


def test_advert_no_reply_proceeds_as_soon_as_activation_is_observed(
    monkeypatch,
):
    calls = []
    counts = iter([0, 0, 0, 1])
    sleeps = []
    elapsed = 0.0

    def monotonic():
        return elapsed

    def sleep(seconds):
        nonlocal elapsed
        sleeps.append(seconds)
        elapsed += seconds

    class Manager:
        def RegisterAdvertisement(self, path, options, **kwargs):
            calls.append((path, options, kwargs))
            raise dbus.exceptions.DBusException(
                "method reply timed out",
                name="org.freedesktop.DBus.Error.NoReply",
            )

    monkeypatch.setattr(bluez_setup, "_advert_instance", object())
    monkeypatch.setattr(bluez_setup, "_advert_registered", False)
    monkeypatch.setattr(bluez_setup, "bluez", lambda *_args: Manager())
    monkeypatch.setattr(
        bluez_setup,
        "_active_advertisements",
        lambda _adapter=None: next(counts),
    )
    monkeypatch.setattr(bluez_setup.time, "monotonic", monotonic)
    monkeypatch.setattr(bluez_setup.time, "sleep", sleep)

    assert bluez_setup.register_advert("hci7") is True
    assert calls[0][2]["timeout"] == 1.0
    assert sleeps == [0.25, 0.25]
    assert elapsed < bluez_setup.ADVERT_ACTIVATION_TIMEOUT_SECONDS


def test_advert_activation_polling_keeps_a_bounded_failure_deadline(monkeypatch):
    elapsed = 0.0

    class Manager:
        def RegisterAdvertisement(self, _path, _options, **_kwargs):
            raise dbus.exceptions.DBusException(
                "method reply timed out",
                name="org.freedesktop.DBus.Error.NoReply",
            )

    def sleep(seconds):
        nonlocal elapsed
        elapsed += seconds

    monkeypatch.setattr(bluez_setup, "_advert_instance", object())
    monkeypatch.setattr(bluez_setup, "_advert_registered", False)
    monkeypatch.setattr(bluez_setup, "bluez", lambda *_args: Manager())
    monkeypatch.setattr(bluez_setup, "_active_advertisements", lambda _adapter=None: 0)
    monkeypatch.setattr(bluez_setup.time, "monotonic", lambda: elapsed)
    monkeypatch.setattr(bluez_setup.time, "sleep", sleep)

    assert bluez_setup.register_advert("hci7") is False
    assert elapsed == bluez_setup.ADVERT_ACTIVATION_TIMEOUT_SECONDS
