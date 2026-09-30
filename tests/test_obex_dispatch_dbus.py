"""Worker retries and transfer signals on an isolated, activation-free bus."""
from __future__ import annotations

import gc
import threading
import time
import weakref

import dbus
import dbus.service
import pytest
from gi.repository import GLib

from blueferry import bus
from blueferry.errors import SendOutcomeUnknownError
from blueferry.obex import map_send, transfer
from blueferry.obex.sessions import SessionManager
from tests.private_bus import open_private_bus

pytestmark = pytest.mark.private_dbus


@pytest.mark.parametrize('cleanup_fails', [False, True])
def test_failed_transfer_subscription_releases_watches_and_preserves_other_receivers(
    monkeypatch, cleanup_fails,
):
    observer = open_private_bus()
    observer.request_name('org.bluez.obex', dbus.bus.NAME_FLAG_DO_NOT_QUEUE)
    monkeypatch.setattr(transfer, 'get_session_bus', lambda: observer)
    original_add_match = observer.add_match_string
    references = []
    # Capture a weak reference without retaining exception tracebacks or
    # relying on dbus-python's internal receiver containers.
    original_subscribe = transfer.TransferStatusWatch._subscribe

    def subscribe(self):
        references.append(weakref.ref(self))
        return original_subscribe(self)

    monkeypatch.setattr(transfer.TransferStatusWatch, '_subscribe', subscribe)
    unrelated_received = threading.Event()
    unrelated = observer.add_signal_receiver(
        lambda *_args, **_kwargs: unrelated_received.set(),
        signal_name='PropertiesChanged', dbus_interface='org.freedesktop.DBus.Properties',
        bus_name='org.bluez.obex', arg0='org.bluez.obex.Transfer1', path_keyword='path',
    )

    def reject_transfer_rule(rule):
        if "arg0='org.bluez.obex.Transfer1'" in rule:
            raise dbus.exceptions.DBusException(
                'simulated match-rule limit', name='org.freedesktop.DBus.Error.LimitsExceeded',
            )
        return original_add_match(rule)

    monkeypatch.setattr(observer, 'add_match_string', reject_transfer_rule)
    original_remove_match = observer.remove_match_string_non_blocking
    if cleanup_fails:
        def reject_cleanup(_rule):
            raise dbus.exceptions.DBusException(
                'simulated cleanup failure', name='org.freedesktop.DBus.Error.Disconnected',
            )
        monkeypatch.setattr(observer, 'remove_match_string_non_blocking', reject_cleanup)

    def failed_attempt():
        try:
            transfer.TransferStatusWatch('/session')
        except dbus.exceptions.DBusException as error:
            assert error.get_dbus_name() == 'org.freedesktop.DBus.Error.LimitsExceeded'
        else:
            pytest.fail('subscription unexpectedly succeeded')

    try:
        for _ in range(3):
            failed_attempt()
        gc.collect()
        assert all(reference() is None for reference in references)
        # An unrelated, healthy receiver on the shared bus remains usable.
        monkeypatch.setattr(observer, 'add_match_string', original_add_match)
        monkeypatch.setattr(observer, 'remove_match_string_non_blocking', original_remove_match)
        watch = transfer.TransferStatusWatch('/session')
        watch.close()
        assert observer.get_is_connected()
        signal = dbus.lowlevel.SignalMessage(
            '/session/transfer1', 'org.freedesktop.DBus.Properties', 'PropertiesChanged',
        )
        signal.append('org.bluez.obex.Transfer1', {'Status': 'complete'}, [], signature='sa{sv}as')
        observer.send_message(signal)
        context = GLib.MainContext.default()
        deadline = time.monotonic() + 5
        while not unrelated_received.is_set() and time.monotonic() < deadline:
            context.iteration(False)
            time.sleep(0.001)
        assert unrelated_received.is_set()
    finally:
        monkeypatch.setattr(observer, 'remove_match_string_non_blocking', original_remove_match)
        unrelated.remove()
        observer.close()


@pytest.mark.parametrize('status', ['complete', 'error', None])
def test_worker_retries_keep_transfer_evidence_on_main_thread(monkeypatch, status):
    service_bus = open_private_bus()
    service_bus.request_name('org.bluez.obex', dbus.bus.NAME_FLAG_DO_NOT_QUEUE)
    objects = []
    creations = []
    subscriptions = []
    calls = []

    # Production opens this connection; conftest's guard wraps SessionBus.
    session_bus = getattr(dbus.SessionBus, '__wrapped__', dbus.SessionBus)

    def connect(*, private, mainloop):
        creations.append((threading.get_ident(), mainloop))
        connection = session_bus(private=private, mainloop=mainloop)
        connection.set_exit_on_disconnect(False)
        return connection

    def observer_bus():
        subscriptions.append(threading.get_ident())
        return bus.get_session_bus()

    monkeypatch.setattr(bus.dbus, 'SessionBus', connect)
    monkeypatch.setattr(transfer, 'get_session_bus', observer_bus)

    class FastTransfer(dbus.service.Object):
        @dbus.service.signal('org.freedesktop.DBus.Properties', signature='sa{sv}as')
        def PropertiesChanged(self, _interface, _changed, _invalidated):
            pass

        @dbus.service.method('org.freedesktop.DBus.Properties', in_signature='ss', out_signature='v')
        def Get(self, _interface, _property):
            raise dbus.exceptions.DBusException(
                'transfer already removed', name='org.freedesktop.DBus.Error.UnknownObject',
            )

        @dbus.service.method('org.bluez.obex.Transfer1', in_signature='', out_signature='')
        def Cancel(self):
            pass

    class Session(dbus.service.Object):
        def __init__(self, path, owner):
            super().__init__(service_bus, path)
            self.owner = owner

        @dbus.service.method('org.bluez.obex.MessageAccess1', in_signature='', out_signature='s', sender_keyword='sender')
        def CheckOwner(self, sender=None):
            assert sender == self.owner
            return str(sender)

        @dbus.service.method('org.bluez.obex.MessageAccess1', in_signature='ssa{sv}', out_signature='oa{sv}', sender_keyword='sender')
        def PushMessage(self, _source, _folder, _options, sender=None):
            assert sender == self.owner
            path = self.__dbus_object_path__ + '/transfer1'
            outgoing = FastTransfer(service_bus, path)
            objects.append(outgoing)
            # The terminal signal precedes the reply. Polling immediately
            # reports disappearance, so the observer must retain the signal.
            if status is not None:
                outgoing.PropertiesChanged('org.bluez.obex.Transfer1', {'Status': status}, [])
            return dbus.ObjectPath(path), {'Status': 'queued'}

    class Manager(dbus.service.Object):
        @dbus.service.method('org.bluez.obex.Client1', in_signature='sa{sv}', out_signature='o', sender_keyword='sender')
        def CreateSession(self, _destination, options, sender=None):
            path = f'/org/bluez/obex/client/session{len(calls)}'
            calls.append((str(options['Target']), str(sender)))
            objects.append(Session(path, str(sender)))
            return dbus.ObjectPath(path)

    manager = Manager(service_bus, '/org/bluez/obex')
    finished = threading.Event()
    result = {}

    def run():
        sessions = SessionManager()
        sessions.start_monitoring = lambda: None
        try:
            bus.initialize_obex_worker_bus()
            sessions.open_all()
            pbap_owner = str(sessions.pbap.message_access.CheckOwner())
            for _ in range(20):
                sessions.map = None
                sessions.open_all()
                assert str(sessions.pbap.message_access.CheckOwner()) == pbap_owner
            result['path'] = map_send.send_message(sessions.map_path, '+15551234567', 'fixture')
        except Exception as error:
            result['error'] = error
        finally:
            bus.close_obex_worker_bus()
            finished.set()

    worker = threading.Thread(target=run)
    worker.start()
    try:
        context = GLib.MainContext.default()
        deadline = time.monotonic() + 10
        while not finished.is_set() and time.monotonic() < deadline:
            context.iteration(False)
            time.sleep(0.001)
        assert finished.is_set(), 'worker did not finish on isolated D-Bus'
        # Drain the asynchronous main-thread watch removal as well.
        while context.pending():
            context.iteration(False)
        assert subscriptions == [threading.get_ident()]
        assert all(
            mainloop is dbus.mainloop.NULL_MAIN_LOOP
            for owner, mainloop in creations if owner == worker.ident
        )
        assert len({owner for _, owner in calls}) == 22
        assert [target for target, _ in calls].count('PBAP') == 1
        if status == 'complete':
            assert 'error' not in result, result.get('error')
            assert result['path'].endswith('/transfer1')
        elif status == 'error':
            assert isinstance(result.get('error'), transfer.TransferFailed)
        else:
            assert isinstance(result.get('error'), SendOutcomeUnknownError)
    finally:
        worker.join(5)
        assert not worker.is_alive()
        for obj in objects:
            obj.remove_from_connection()
        manager.remove_from_connection()
        service_bus.close()
