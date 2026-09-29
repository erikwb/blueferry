"""Worker retries and transfer signals on an isolated, activation-free bus."""
from __future__ import annotations

import threading
import time

import dbus
import dbus.service
import pytest
from gi.repository import GLib

from blueferry import bus
from blueferry.errors import SendOutcomeUnknownError
from blueferry.obex import map_send, transfer
from blueferry.obex.sessions import SessionManager

pytestmark = pytest.mark.private_dbus


@pytest.mark.parametrize('status', ['complete', 'error', None])
def test_worker_retries_keep_transfer_evidence_on_main_thread(monkeypatch, status):
    service_bus = dbus.SessionBus(private=True)
    service_bus.set_exit_on_disconnect(False)
    service_bus.request_name('org.bluez.obex', dbus.bus.NAME_FLAG_DO_NOT_QUEUE)
    objects = []
    creations = []
    subscriptions = []
    calls = []
    session_bus = dbus.SessionBus

    def connect(*, private, mainloop):
        creations.append((threading.get_ident(), mainloop))
        return session_bus(private=private, mainloop=mainloop)

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
