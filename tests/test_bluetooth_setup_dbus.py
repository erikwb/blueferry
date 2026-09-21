"""Exercise registration callbacks and OBEX owners on an isolated D-Bus."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import dbus
import dbus.service
import pytest
from gi.repository import GLib

from blueferry import bluez_setup, bus
from blueferry import daemon as daemon_mod
from blueferry.obex.sessions import SessionManager
from blueferry.solicitation_supervisor import SolicitationSupervisor

pytestmark = pytest.mark.private_dbus


def dispatch_until(done):
    deadline = time.monotonic() + 5
    context = GLib.MainContext.default()
    while not done() and time.monotonic() < deadline:
        context.iteration(False)
        time.sleep(0.001)
    assert done(), 'isolated D-Bus operation did not finish'


@pytest.fixture
def advertising_manager(monkeypatch):
    service_bus = dbus.SessionBus(private=True)
    service_bus.set_exit_on_disconnect(False)
    service_bus.request_name('org.bluez', dbus.bus.NAME_FLAG_DO_NOT_QUEUE)
    client_bus = dbus.SessionBus(private=True)
    client_bus.set_exit_on_disconnect(False)

    class Manager(dbus.service.Object):
        def __init__(self, connection=service_bus):
            super().__init__(connection, '/org/bluez/hci7')
            self.requests = []
            self.removed = []

        @dbus.service.method('org.bluez.LEAdvertisingManager1', in_signature='oa{sv}',
                             out_signature='', sender_keyword='sender',
                             async_callbacks=('reply', 'error'))
        def RegisterAdvertisement(self, path, _options, reply, error, sender=None):
            # BlueZ must call back into the registering application before
            # it can finish registration. A blocking client deadlocks this.
            props = dbus.Interface(self.connection.get_object(sender, path, introspect=False),
                                   'org.freedesktop.DBus.Properties')
            props.GetAll('org.bluez.LEAdvertisement1', signature='s',
                         reply_handler=lambda values: self.requests.append((str(path), values, reply, error)),
                         error_handler=error)

        @dbus.service.method('org.bluez.LEAdvertisingManager1', in_signature='o', out_signature='')
        def UnregisterAdvertisement(self, path):
            self.removed.append(str(path))

    manager = Manager()
    monkeypatch.setattr(bluez_setup, 'get_system_bus', lambda: client_bus)
    monkeypatch.setattr(bluez_setup, '_advert_instance', None)
    monkeypatch.setattr(bluez_setup, 'PAIRING_ADVERT_SETTLE_SECONDS', 0)
    try:
        yield manager
    finally:
        bluez_setup.unregister_advert('hci7')
        manager.remove_from_connection()
        client_bus.close()
        service_bus.close()


def test_registration_requires_a_reply_after_real_getall_dispatch(advertising_manager):
    manager = advertising_manager
    assert not bluez_setup.register_advert('hci7')
    dispatch_until(lambda: manager.requests)
    path, props, reply, _error = manager.requests[0]
    assert list(props['SolicitUUIDs']) == [bluez_setup.config.ANCS_SOLICIT_UUID]
    assert bluez_setup.advert_registration_pending()
    assert not bluez_setup.advert_registered()
    reply()
    assert bluez_setup.register_advert('hci7', settle_for_pairing=True)
    assert not bluez_setup.advert_registration_pending()
    assert manager.requests[0][0] == path


def test_real_late_failure_does_not_leave_false_registered_state(advertising_manager):
    manager = advertising_manager
    bluez_setup.register_advert('hci7')
    dispatch_until(lambda: manager.requests)
    old_path, _props, _reply, error = manager.requests[0]
    error(dbus.exceptions.DBusException('controller rejected request', name='org.bluez.Error.Failed'))
    dispatch_until(lambda: not bluez_setup.advert_registration_pending())
    assert not bluez_setup.advert_registered()
    dispatch_until(lambda: old_path in manager.removed)
    bluez_setup.register_advert('hci7')
    dispatch_until(lambda: len(manager.requests) == 2)
    assert manager.requests[1][0] != old_path
    manager.requests[1][2]()
    dispatch_until(bluez_setup.advert_registered)


def test_retirement_cancels_real_pending_reply_dispatch(advertising_manager, monkeypatch):
    manager = advertising_manager
    received = []
    registered = bluez_setup._AncsAdvert._registered

    def record_reply(advert):
        received.append(advert.path)
        registered(advert)

    monkeypatch.setattr(bluez_setup._AncsAdvert, '_registered', record_reply)
    bluez_setup.register_advert('hci7')
    assert isinstance(bluez_setup._advert_instance.request, dbus.lowlevel.PendingCall)
    dispatch_until(lambda: manager.requests)
    old_path, _, old_reply, _ = manager.requests[0]
    bluez_setup.unregister_advert('hci7')
    dispatch_until(lambda: old_path in manager.removed)
    old_reply()

    bluez_setup.register_advert('hci7')
    dispatch_until(lambda: len(manager.requests) == 2)
    new_path, _, new_reply, _ = manager.requests[1]
    new_reply()
    dispatch_until(bluez_setup.advert_registered)
    # Both replies were sent on the same service connection. The old reply
    # must be cancelled at D-Bus dispatch, not merely ignored in _registered.
    assert received == [new_path]


@pytest.mark.parametrize('restoring_power', [False, True])
def test_bluez_owner_loss_recreates_advert_only_after_power_restoration(
    advertising_manager, monkeypatch, restoring_power,
):
    manager = advertising_manager
    monkeypatch.setattr(daemon_mod.config, 'ANCS_ENABLED', False)
    monkeypatch.setattr(daemon_mod, 'get_system_bus', bluez_setup.get_system_bus)
    reconnected, invalidated, observed = [], [], []
    value = daemon_mod.Daemon.__new__(daemon_mod.Daemon)
    value._bluez_owner_match = None
    value._bluez_owner_generation = 0
    value._bluetooth_initialized = True
    value.recovery = SimpleNamespace(active=False, invalidate=lambda: invalidated.append(True))
    value.ancs = (
        SimpleNamespace(observe_bluez_owner=lambda old, new: observed.append((str(old), str(new))))
        if restoring_power else None
    )
    value.adapter_class = SimpleNamespace(poke=lambda: None, start=lambda: None)
    value.bearers = SimpleNamespace(
        hold_le=lambda: None, reset_after_bluez_restart=lambda: None, start=lambda: None,
    )
    value.profiles = SimpleNamespace(
        reconnect=lambda *args, **kwargs: reconnected.append((args, kwargs)),
        resume=lambda: reconnected.append('resume'),
    )
    value.solicitation = SolicitationSupervisor(
        'hci7', schedule=lambda *_args: 1, cancel=lambda _timer: None,
    )
    replacement_bus = dbus.SessionBus(private=True)
    replacement_bus.set_exit_on_disconnect(False)
    replacement = type(manager)(replacement_bus)
    try:
        value._watch_bluez_owner()
        value.solicitation.start()
        dispatch_until(lambda: manager.requests)
        old_path, _props, reply, _error = manager.requests[0]
        reply()
        dispatch_until(bluez_setup.advert_registered)

        if restoring_power:
            value.recovery.active = True
            value.solicitation.stop()
            dispatch_until(lambda: old_path in manager.removed)

        # A crashed bluetoothd cannot call Release. Losing its well-known
        # name generates the same owner-loss signal without that callback.
        manager.connection.release_name('org.bluez')
        dispatch_until(lambda: len(invalidated) == 1)
        assert not bluez_setup.advert_registered()
        assert not reconnected  # Wait for a replacement before reconnecting.
        replacement_bus.request_name('org.bluez', dbus.bus.NAME_FLAG_DO_NOT_QUEUE)
        dispatch_until(lambda: len(invalidated) == 2)
        if restoring_power:
            assert observed == [
                (manager.connection.get_unique_name(), ''),
                ('', replacement_bus.get_unique_name()),
            ]
            assert not replacement.requests
            assert not bluez_setup.advert_registration_pending()
            assert not reconnected
            value.recovery.active = False
            value._resume_after_recovery()
        dispatch_until(lambda: replacement.requests)
        new_path, _props, reply, _error = replacement.requests[0]
        assert new_path != old_path
        reply()
        dispatch_until(bluez_setup.advert_registered)
        expected = ['resume'] if restoring_power else [
            (('bluetoothd restarted',), {'remove_remote_sessions': False}),
        ]
        assert reconnected == expected
        value.solicitation.stop()
        dispatch_until(lambda: new_path in replacement.removed)
    finally:
        match, value._bluez_owner_match = value._bluez_owner_match, None
        if match is not None:
            match.remove()
        value.solicitation.stop()
        replacement.remove_from_connection()
        replacement_bus.close()


def test_real_obex_retry_releases_only_its_profile_owner():
    service_bus = dbus.SessionBus(private=True)
    service_bus.set_exit_on_disconnect(False)
    service_bus.request_name('org.bluez.obex', dbus.bus.NAME_FLAG_DO_NOT_QUEUE)
    lost, calls = set(), []

    class Session(dbus.service.Object):
        def __init__(self, path, owner):
            super().__init__(service_bus, path)
            self.owner = owner

        @dbus.service.method('org.bluez.obex.MessageAccess1', in_signature='', out_signature='s', sender_keyword='sender')
        def CheckOwner(self, sender=None):
            assert sender == self.owner
            return str(sender)

    objects = []

    class Manager(dbus.service.Object):
        @dbus.service.method('org.bluez.obex.Client1', in_signature='sa{sv}', out_signature='o', sender_keyword='sender')
        def CreateSession(self, _destination, options, sender=None):
            path = f'/org/bluez/obex/client/session{len(calls)}'
            calls.append((str(options['Target']), str(sender), path))
            objects.append(Session(path, str(sender)))
            return dbus.ObjectPath(path)

    manager = Manager(service_bus, '/org/bluez/obex')
    match = service_bus.add_signal_receiver(
        lambda changed, _old, new: lost.add(str(changed)) if not new else None,
        signal_name='NameOwnerChanged', dbus_interface='org.freedesktop.DBus',
    )
    finished = threading.Event()
    result = {}

    def run():
        sessions = SessionManager()
        sessions.start_monitoring = lambda: None
        try:
            sessions.open_all()
            pbap = sessions.pbap
            sessions.map = None
            sessions.open_all()
            assert sessions.pbap is pbap
            result['owner'] = str(sessions.map.message_access.CheckOwner())
            # Leave PBAP connected until assertions on the main thread finish.
            finished.set()
            assert release.wait(5)
        except Exception as error:
            result['error'] = error
            finished.set()
        finally:
            bus.close_obex_worker_bus()

    release = threading.Event()
    thread = threading.Thread(target=run)
    try:
        thread.start()
        dispatch_until(finished.is_set)
        assert 'error' not in result, result.get('error')
        assert [target for target, _, _ in calls] == ['MAP', 'PBAP', 'MAP']
        old_map, pbap, new_map = calls
        assert len({old_map[1], pbap[1], new_map[1]}) == 3
        dispatch_until(lambda: old_map[1] in lost)
        assert pbap[1] not in lost
        assert new_map[1] not in lost
        assert result['owner'] == new_map[1]
    finally:
        release.set()
        thread.join(5)
        assert not thread.is_alive()
        match.remove()
        for obj in objects:
            obj.remove_from_connection()
        manager.remove_from_connection()
        service_bus.close()
