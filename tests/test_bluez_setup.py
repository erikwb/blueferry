"""BLE advertisement shape and cleanup regressions."""
from __future__ import annotations

from types import SimpleNamespace

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


def test_authorized_cod_change_uses_packaged_systemd_unit(monkeypatch):
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
        "/usr/bin/systemctl",
        "start",
        "blueferry-btmgmt-set-class@7.service",
    ]
    assert calls[0][1]["timeout"] == 120
    assert calls[0][1]["env"]["LC_ALL"] == "C"


@pytest.mark.parametrize(
    "stderr",
    [
        "Failed to start unit: Interactive authentication required.",
        "Error: No authentication agent found.",
    ],
)
def test_cod_change_explains_when_polkit_authentication_is_unavailable(
    monkeypatch, stderr,
):
    monkeypatch.setattr(bluez_setup.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(bluez_setup.os.path, "isfile", lambda _path: True)
    monkeypatch.setattr(bluez_setup.os, "access", lambda _path, _mode: True)
    monkeypatch.setattr(
        bluez_setup,
        "run_command",
        lambda _args, **_kwargs: type(
            "Result",
            (),
            {"returncode": 1, "stdout": "", "stderr": stderr},
        )(),
    )

    with pytest.raises(
        bluez_setup.PairingError,
        match="No Polkit authentication is available to set device class",
    ) as failure:
        bluez_setup.set_cod(adapter="hci7", authorize=True)

    assert str(failure.value) == bluez_setup.POLKIT_UNAVAILABLE_MESSAGE


def test_cod_change_explains_when_the_packaged_systemd_unit_is_missing(monkeypatch):
    monkeypatch.setattr(bluez_setup.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(bluez_setup.os.path, "isfile", lambda _path: True)
    monkeypatch.setattr(bluez_setup.os, "access", lambda _path, _mode: True)
    monkeypatch.setattr(
        bluez_setup,
        "run_command",
        lambda _args, **_kwargs: type(
            "Result",
            (),
            {
                "returncode": 1,
                "stdout": "",
                "stderr": (
                    "Failed to start blueferry-btmgmt-set-class@7.service: "
                    "Unit blueferry-btmgmt-set-class@7.service not found."
                ),
            },
        )(),
    )

    with pytest.raises(bluez_setup.PairingError) as failure:
        bluez_setup.set_cod(adapter="hci7", authorize=True)

    assert str(failure.value) == bluez_setup.DEVICE_CLASS_SERVICE_MISSING_MESSAGE


def test_cod_change_does_not_mislabel_an_unrelated_systemctl_failure(monkeypatch):
    monkeypatch.setattr(bluez_setup.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(bluez_setup.os.path, "isfile", lambda _path: True)
    monkeypatch.setattr(bluez_setup.os, "access", lambda _path, _mode: True)
    monkeypatch.setattr(
        bluez_setup,
        "run_command",
        lambda _args, **_kwargs: type(
            "Result",
            (),
            {"returncode": 1, "stdout": "", "stderr": "Job failed"},
        )(),
    )

    assert bluez_setup.set_cod(adapter="hci7", authorize=True) is False


def test_cod_change_rejects_an_invalid_adapter(monkeypatch):
    calls = []
    monkeypatch.setattr(
        bluez_setup,
        "run_command",
        lambda args, **_kwargs: calls.append(args),
    )

    assert bluez_setup.set_cod(adapter="hci0/../../evil", authorize=True) is False
    assert calls == []


@pytest.fixture
def adverts(monkeypatch):
    now = [0.0]
    requests, removed, dispatched = [], [], []

    class Manager:
        def RegisterAdvertisement(self, path, options, **kwargs):
            request = SimpleNamespace(path=str(path), options=options, **kwargs)
            request.cancelled = False
            def cancel():
                request.cancelled = True
            request.cancel = cancel
            requests.append(request)
            return request

        def UnregisterAdvertisement(self, path, **kwargs):
            assert kwargs['signature'] == 'o'
            removed.append(str(path))

    context = SimpleNamespace(iteration=lambda _block: dispatched.pop(0)() if dispatched else None)
    monkeypatch.setattr(bluez_setup, '_advert_instance', None)
    monkeypatch.setattr(bluez_setup, 'get_system_bus', lambda: SimpleNamespace(get_object=lambda *a, **k: None))
    monkeypatch.setattr(bluez_setup.dbus, 'Interface', lambda *a: Manager())
    monkeypatch.setattr(bluez_setup.dbus.service.Object, '__init__', lambda *a: None)
    monkeypatch.setattr(bluez_setup.dbus.service.Object, 'remove_from_connection', lambda *a: None)
    monkeypatch.setattr(bluez_setup.GLib.MainContext, 'default', lambda: context)
    monkeypatch.setattr(bluez_setup.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(bluez_setup.time, 'sleep', lambda delay: now.__setitem__(0, now[0] + delay))
    yield SimpleNamespace(now=now, requests=requests, removed=removed, dispatched=dispatched)
    bluez_setup.forget_advert_registration()


def test_pending_advert_is_not_active_and_registration_is_not_duplicated(adverts):
    assert not bluez_setup.register_advert('hci7')
    assert bluez_setup.advert_registration_pending()
    assert not bluez_setup.advert_registered()
    assert not bluez_setup.register_advert('hci7')
    assert len(adverts.requests) == 1
    request = adverts.requests[0]
    assert request.signature == 'oa{sv}'
    request.reply_handler()
    assert bluez_setup.advert_registered()
    assert not bluez_setup.advert_registration_pending()
    assert bluez_setup.register_advert('hci7')
    assert len(adverts.requests) == 1


@pytest.mark.parametrize('error_name', [
    'org.bluez.Error.Failed',
    'org.bluez.Error.AlreadyExists',
    'org.freedesktop.DBus.Error.NoReply',
])
def test_late_registration_failure_is_retried_with_a_new_path(adverts, error_name):
    bluez_setup.register_advert('hci7')
    old = adverts.requests[0]
    old.error_handler(dbus.exceptions.DBusException('failed', name=error_name))
    assert not bluez_setup.advert_registered()
    assert not bluez_setup.advert_registration_pending()
    assert adverts.removed == [old.path]
    bluez_setup.register_advert('hci7')
    new = adverts.requests[1]
    assert new.path != old.path
    old.reply_handler()  # A late completion must not resurrect the old request.
    assert not bluez_setup.advert_registered()
    new.reply_handler()
    assert bluez_setup.advert_registered()


def test_stopping_cancels_pending_advert_and_ignores_late_reply(adverts):
    bluez_setup.register_advert('hci7')
    request = adverts.requests[0]
    bluez_setup.unregister_advert('hci7')
    assert request.cancelled
    assert adverts.removed == [request.path]
    request.reply_handler()
    assert not bluez_setup.advert_registered()


def test_old_release_cannot_clear_a_new_registration(adverts):
    bluez_setup.register_advert('hci7')
    previous = bluez_setup._advert_instance
    bluez_setup.forget_advert_registration()
    assert adverts.removed == []  # Do not unregister against a new BlueZ owner.
    bluez_setup.register_advert('hci7')
    adverts.requests[-1].reply_handler()
    previous.Release()
    assert bluez_setup.advert_registered()
    bluez_setup._advert_instance.Release()
    assert not bluez_setup.advert_registered()


def test_pairing_dispatches_registration_and_waits_after_confirmation(adverts):
    adverts.dispatched.append(lambda: adverts.requests[0].reply_handler())
    assert bluez_setup.register_advert('hci7', settle_for_pairing=True)
    assert adverts.now[0] >= bluez_setup.PAIRING_ADVERT_SETTLE_SECONDS
    assert adverts.now[0] < bluez_setup.PAIRING_ADVERT_SETTLE_SECONDS + 0.2


def test_pairing_registration_deadline_cleans_up_an_unanswered_request(adverts):
    assert not bluez_setup.register_advert('hci7', settle_for_pairing=True)
    assert bluez_setup.ADVERT_ACTIVATION_TIMEOUT_SECONDS <= adverts.now[0] < 15.2
    assert adverts.requests[0].cancelled
    assert adverts.removed == [adverts.requests[0].path]
    adverts.requests[0].reply_handler()
    assert not bluez_setup.advert_registered()
