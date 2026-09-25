"""The persisted pairing policy controls long-lived ANCS work."""

from types import SimpleNamespace

import pytest

from blueferry import daemon


class _Bearer:
    le_state = False

    def __init__(self, calls):
        self.calls = calls

    def start(self):
        self.calls.append("bearers-start")

    def hold_le(self):
        self.calls.append("bearers-hold-le")

    def reset_after_bluez_restart(self):
        self.calls.append("bearers-reset")

    def recover_le_transport(self):
        self.calls.append("bearers-recover-le")

class _Events:
    def __init__(self, calls):
        self.calls = calls

    def setup(self):
        self.calls.append("events-setup")

    @staticmethod
    def ancs(_event):
        return None


class _Profiles:
    ready = True

    def __init__(self, calls):
        self.calls = calls

    def start(self):
        self.calls.append("profiles-start")

    def reconnect(self, reason, *, remove_remote_sessions=True):
        self.calls.append(
            ("profiles-reconnect", reason, remove_remote_sessions)
        )


class _Solicitation:
    def __init__(self, calls):
        self.calls = calls

    def start(self):
        self.calls.append("solicitation-start")

    def set_needed(self, needed):
        self.calls.append(("solicitation-needed", needed))

    def reset_after_bluez_restart(self):
        self.calls.append("solicitation-reset")


class _AdapterClass:
    def __init__(self, calls):
        self.calls = calls

    def start(self):
        self.calls.append("adapter-class-start")

    def poke(self):
        self.calls.append("adapter-class-poke")


def _mns_watch(calls):
    class Watch:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            calls.append("mns-watch-start")

        def stop(self):
            calls.append("mns-watch-stop")

    return Watch


def _daemon(calls):
    value = daemon.Daemon.__new__(daemon.Daemon)
    value.recovery = SimpleNamespace(
        active=False,
        adapter=SimpleNamespace(restore_pending=False, cleanup_pending=False),
        start=lambda: calls.append("recovery-start"),
        stop=lambda: None,
        invalidate=lambda **_kwargs: calls.append("recovery-invalidate"),
    )
    value.bearers = _Bearer(calls)
    value.events = _Events(calls)
    value.profiles = _Profiles(calls)
    value.adapter_class = _AdapterClass(calls)
    value.solicitation = _Solicitation(calls)
    value.phone_audio = type(
        "Audio", (), {"reconcile": lambda self, **_kwargs: False}
    )()
    value.notification_policy = type(
        "Policy", (), {"value": "messages", "contacts_only": False}
    )()
    value.setup_verification = type("Verification", (), {"verified": ()})()
    value.ancs = None
    value._dbus_service = None
    value._bluez_owner_match = None
    value._bluez_owner_generation = 0
    value._watch_sleep_resume = lambda: calls.append("sleep-watch")
    value._bluetooth_initialized = True
    value._initialization_retry_id = None
    return value


def _ready_bluetooth(monkeypatch, calls):
    watches = []

    def watch(callback, **kwargs):
        match = SimpleNamespace(remove=lambda: None)
        watches.append((callback, kwargs, match))
        return match

    monkeypatch.setattr(daemon, 'get_system_bus', lambda: SimpleNamespace(add_signal_receiver=watch))
    monkeypatch.setattr(daemon, "bond_status", lambda *_args: True)
    monkeypatch.setattr(
        daemon.bluez_setup,
        "prepare_classic",
        lambda: calls.append("solicitation-prepare") or True,
    )
    return watches


def test_compatibility_daemon_solicits_but_never_starts_ancs(monkeypatch):
    calls = []
    value = _daemon(calls)
    _ready_bluetooth(monkeypatch, calls)
    monkeypatch.setattr(daemon.config, "ANCS_ENABLED", False)
    monkeypatch.setattr(
        daemon,
        "AncsClient",
        lambda *_args, **_kwargs: calls.append("unexpected-ancs-client"),
    )

    value._initialize_bluetooth()

    assert calls == [
        "adapter-class-start",
        "solicitation-prepare",
        "solicitation-start",
        "bearers-start",
        "sleep-watch",
        "events-setup",
        "profiles-start",
    ]
    assert value.ancs is None


def test_full_daemon_starts_ancs_client(monkeypatch):
    calls = []

    def app_filter(app_id):
        return app_id == "com.example.Allowed"

    value = _daemon(calls)
    _ready_bluetooth(monkeypatch, calls)
    monkeypatch.setattr(daemon.config, "ANCS_ENABLED", True)
    monkeypatch.setattr(daemon.config, "include_ancs_app", app_filter)

    class Ancs:
        def __init__(self, *_args, **kwargs):
            calls.append("ancs-client")
            calls.append(("previously-authorized", kwargs["previously_authorized"]))
            calls.append(("app-filter", kwargs["include_app_notification"]))

        def observe_bearer_state(self, connected):
            calls.append(("ancs-bearer", connected))

        def start(self):
            calls.append("ancs-start")

        def stop(self):
            calls.append("ancs-stop")

    monkeypatch.setattr(daemon, "AncsClient", Ancs)

    value._initialize_bluetooth()

    assert "ancs-client" in calls
    assert ("previously-authorized", False) in calls
    assert ("app-filter", app_filter) in calls
    assert ("ancs-bearer", False) in calls
    assert "ancs-start" in calls
    assert value.ancs is not None


def test_full_daemon_preserves_known_ancs_reconnect_protection(monkeypatch):
    calls = []
    value = _daemon(calls)
    value.setup_verification = type(
        "Verification",
        (),
        {"verified": (daemon.NOTIFICATION_ACCESS,)},
    )()
    _ready_bluetooth(monkeypatch, calls)
    monkeypatch.setattr(daemon.config, "ANCS_ENABLED", True)

    class Ancs:
        def __init__(self, *_args, **kwargs):
            calls.append(("previously-authorized", kwargs["previously_authorized"]))

        @staticmethod
        def observe_bearer_state(_connected):
            return None

        @staticmethod
        def start():
            return None

        @staticmethod
        def stop():
            return None

    monkeypatch.setattr(daemon, "AncsClient", Ancs)

    value._initialize_bluetooth()

    assert ("previously-authorized", True) in calls


def test_bluez_restart_reapplies_profile_gate_before_resetting_bearers():
    calls = []
    value = _daemon(calls)

    value._on_bluez_restart()

    assert calls == [
        "adapter-class-poke",
        "bearers-hold-le",
        ("profiles-reconnect", "bluetoothd restarted", False),
        "bearers-reset",
    ]


def test_recovery_observation_excludes_permissions_and_missing_profiles(monkeypatch):
    calls = []
    value = _daemon(calls)
    value._initializing = False
    value.bearers.bredr_connected = True
    value.bearers.busy = False
    value.solicitation.active = lambda: True
    value.ancs = SimpleNamespace(connected=False, health_proof=123.0, permission_denied=False)
    monkeypatch.setattr(daemon.config, "ANCS_ENABLED", True)
    assert value._recovery_observation().eligible
    value.ancs.permission_denied = True
    assert not value._recovery_observation().eligible
    value.ancs.permission_denied = False
    value.profiles.ready = False
    assert not value._recovery_observation().eligible
    value.profiles.ready = True
    monkeypatch.setattr(daemon.config, "ANCS_ENABLED", False)
    assert not value._recovery_observation().eligible


def test_own_power_events_and_owner_changes_do_not_start_parallel_recovery(monkeypatch):
    calls = []
    value = _daemon(calls)
    _ready_bluetooth(monkeypatch, calls)
    value._watch_bluez_owner()
    monkeypatch.setattr(daemon.bluez_setup, 'forget_advert_registration', lambda: None)
    value.recovery.active = True
    value._on_adapter_power_changed("org.bluez.Adapter1", {"Powered": False}, [])
    assert calls == []
    value._on_bluez_owner_changed('org.bluez', ':1.1', ':1.2')
    assert calls == ["recovery-invalidate"]
    value.recovery.active = False
    value._on_adapter_power_changed("org.bluez.Adapter1", {"Powered": False}, [])
    assert calls == ["recovery-invalidate", "recovery-invalidate"]


def test_wake_does_not_resume_profiles_while_power_restoration_is_pending():
    calls = []
    value = _daemon(calls)
    value.recovery.active = True
    value.bearers.poke = lambda: calls.append("bearers-poke")
    value._on_prepare_for_sleep(False)
    assert calls == ["recovery-invalidate"]


def test_startup_waits_for_saved_restoration_before_any_bluetooth_setup(monkeypatch):
    calls = []
    value = _daemon(calls)
    value._bluetooth_initialized = False
    value.recovery.adapter.restore_pending = True
    # Restoring an already-issued operation also applies if ANCS was disabled
    # between the previous daemon's shutdown and this startup.
    monkeypatch.setattr(daemon.config, "ANCS_ENABLED", False)
    value._initialize_bluetooth()
    assert calls == ["recovery-start"]
    value._pause_for_recovery()
    assert calls == ["recovery-start"]
    scheduled = []
    monkeypatch.setattr(daemon.GLib, "idle_add", lambda callback: scheduled.append(callback) or 42)
    value.recovery.adapter.restore_pending = False
    value._resume_after_recovery()
    value._resume_after_recovery()
    assert scheduled == [value._initialize]
    assert value._initialization_retry_id == 42
    assert calls == ["recovery-start"]


def test_compatibility_startup_keeps_journal_cleanup_running_without_delaying_profiles(monkeypatch):
    calls = []
    value = _daemon(calls)
    value.recovery.adapter.cleanup_pending = True
    value.recovery.stop = lambda: calls.append("recovery-stop")
    _ready_bluetooth(monkeypatch, calls)
    monkeypatch.setattr(daemon.config, "ANCS_ENABLED", False)

    value._initialize_bluetooth()

    assert calls[0] == "recovery-start"
    assert "profiles-start" in calls
    assert "recovery-stop" not in calls
    assert value.ancs is None


def test_recovery_pause_and_resume_keep_map_first_order():
    value = _daemon([])
    calls = []
    value.adapter_class = SimpleNamespace(
        stop=lambda: calls.append("class-stop"), start=lambda: calls.append("class-start"),
    )
    value.bearers = SimpleNamespace(
        stop=lambda: calls.append("bearers-stop"), start=lambda: calls.append("bearers-start"),
        hold_le=lambda: calls.append("hold-le"),
        reset_after_bluez_restart=lambda: calls.append("bearers-reset"),
    )
    value.profiles = SimpleNamespace(
        pause=lambda: calls.append("profiles-pause"),
        resume=lambda: calls.append("profiles-resume"),
    )
    value.solicitation = SimpleNamespace(
        stop=lambda: calls.append("advert-stop"), start=lambda: calls.append("advert-start"),
    )
    value.sessions = SimpleNamespace(close_all=lambda **kw: calls.append(("forget", kw)))
    value.ancs = SimpleNamespace(observe_bearer_state=lambda state: calls.append(("ancs", state)))
    value._pause_for_recovery()
    value._resume_after_recovery()
    assert calls == [
        "class-stop", "bearers-stop", "profiles-pause", "advert-stop",
        ("forget", {"remove_remote": False}), ("ancs", False),
        "class-start", "advert-start", "hold-le", "bearers-reset",
        "profiles-resume", "bearers-start",
    ]


@pytest.mark.parametrize('ancs_enabled', [False, True])
@pytest.mark.parametrize('split_change', [False, True])
def test_bluez_owner_watch_recovers_once_in_both_pairing_modes(monkeypatch, ancs_enabled, split_change):
    calls = []
    value = _daemon(calls)
    watches = _ready_bluetooth(monkeypatch, calls)
    monkeypatch.setattr(daemon.config, 'ANCS_ENABLED', ancs_enabled)
    monkeypatch.setattr(daemon.bluez_setup, 'forget_advert_registration', lambda: calls.append('forget-advert'))
    monkeypatch.setattr(daemon, 'AncsClient', lambda *_args, **_kwargs: SimpleNamespace(
        observe_bearer_state=lambda _state: None,
        start=lambda: None,
        observe_bluez_owner=lambda old, new: calls.append(('ancs-owner', old, new)),
    ))
    value._initialize_bluetooth()
    value._initialize_bluetooth()
    assert len(watches) == 1
    callback, filters, _match = watches[0]
    assert filters == {
        'dbus_interface': 'org.freedesktop.DBus',
        'signal_name': 'NameOwnerChanged',
        'bus_name': 'org.freedesktop.DBus',
        'arg0': 'org.bluez',
    }
    calls.clear()
    if split_change:
        callback('org.bluez', ':1.1', '')
        callback('org.bluez', '', ':1.2')
    else:
        callback('org.bluez', ':1.1', ':1.2')
    expected = ['recovery-invalidate', 'forget-advert']
    if ancs_enabled and split_change:
        expected += [('ancs-owner', ':1.1', '')]
    if split_change:
        expected += ['recovery-invalidate']
    expected += ['solicitation-reset']
    if ancs_enabled:
        expected += [('ancs-owner', '' if split_change else ':1.1', ':1.2')]
    assert calls == expected + [
        'adapter-class-poke', 'bearers-hold-le',
        ('profiles-reconnect', 'bluetoothd restarted', False), 'bearers-reset',
    ]


def test_nested_owner_loss_during_ancs_rescan_does_not_restart_absent_bluez(monkeypatch):
    calls = []
    value = _daemon(calls)
    _ready_bluetooth(monkeypatch, calls)
    value._watch_bluez_owner()
    monkeypatch.setattr(daemon.bluez_setup, 'forget_advert_registration', lambda: None)

    def rescan(_old, new):
        if new:
            value._on_bluez_owner_changed('org.bluez', new, '')

    value.ancs = SimpleNamespace(observe_bluez_owner=rescan)
    value._on_bluez_owner_changed('org.bluez', ':1.1', ':1.2')
    assert calls == ['recovery-invalidate', 'solicitation-reset', 'recovery-invalidate']


def test_owner_change_during_initial_ancs_scan_is_forwarded(monkeypatch):
    calls, observed = [], []
    value = _daemon(calls)
    watches = _ready_bluetooth(monkeypatch, calls)
    monkeypatch.setattr(daemon.config, 'ANCS_ENABLED', True)
    monkeypatch.setattr(daemon.bluez_setup, 'forget_advert_registration', lambda: None)

    def start():
        watches[0][0]('org.bluez', ':1.1', ':1.2')

    candidate = SimpleNamespace(
        observe_bearer_state=lambda _state: None,
        start=start,
        observe_bluez_owner=lambda old, new: observed.append((old, new)),
    )
    monkeypatch.setattr(daemon, 'AncsClient', lambda *_args, **_kwargs: candidate)
    value._initialize_bluetooth()
    assert observed == [(':1.1', ':1.2')]
    assert calls.count('bearers-reset') == 1
    assert value.ancs is candidate


def test_failed_ancs_start_can_retry_without_leaking_an_owner_watch(monkeypatch):
    calls, started, stopped = [], [], []
    value = _daemon(calls)
    watches = _ready_bluetooth(monkeypatch, calls)
    monkeypatch.setattr(daemon.config, 'ANCS_ENABLED', True)

    def start():
        started.append(True)
        if len(started) == 1:
            raise RuntimeError('ObjectManager unavailable')

    monkeypatch.setattr(daemon, 'AncsClient', lambda *_args, **_kwargs: SimpleNamespace(
        observe_bearer_state=lambda _state: None,
        start=start,
        stop=lambda: stopped.append(True),
    ))
    with pytest.raises(RuntimeError, match='ObjectManager unavailable'):
        value._initialize_bluetooth()
    assert value.ancs is None
    assert stopped == [True]
    value._initialize_bluetooth()
    assert value.ancs is not None
    assert len(started) == 2
    assert len(watches) == 1


def test_solicitation_stays_up_until_profiles_and_ancs_are_ready(monkeypatch):
    calls = []
    value = _daemon(calls)
    monkeypatch.setattr(daemon.config, "ANCS_ENABLED", True)
    value.ancs = type("Ancs", (), {"connected": True})()

    value.profiles.ready = False
    value._sync_solicitation()
    value.profiles.ready = True
    value._sync_solicitation()

    assert calls == [
        ("solicitation-needed", True),
        ("solicitation-needed", False),
    ]


def test_partial_pbap_starts_contacts_without_map_listener(monkeypatch):
    calls = []
    value = daemon.Daemon.__new__(daemon.Daemon)
    value.sessions = type("Sessions", (), {"map": None, "pbap": object()})()
    value.contacts = type(
        "Contacts",
        (),
        {"count": lambda _self: 0, "resolve": lambda _self, raw: raw},
    )()
    value._contacts_refresh_id = None
    value._contacts_refresh_deferred = False
    value._contacts_initial_sync_done = False
    value.listener = None
    value._refresh_contacts = lambda: calls.append("refresh-contacts")
    value._periodic_refresh_contacts = lambda: True
    monkeypatch.setattr(
        daemon.GLib,
        "timeout_add_seconds",
        lambda delay, _callback: calls.append(("schedule", delay)) or 77,
    )
    monkeypatch.setattr(
        daemon,
        "MapEventListener",
        lambda **_kwargs: calls.append("unexpected-map-listener"),
    )

    value._post_available_sessions_setup()

    assert calls == [
        "refresh-contacts",
        ("schedule", daemon.CONTACTS_REFRESH_SEC),
    ]
    assert value._contacts_refresh_id == 77
    assert value.listener is None


def test_partial_map_starts_listener_without_contacts_work(monkeypatch):
    calls = []

    class Listener:
        def __init__(self, **_kwargs):
            calls.append("map-listener")

        def start(self):
            calls.append("map-listener-start")

    value = daemon.Daemon.__new__(daemon.Daemon)
    value.sessions = type("Sessions", (), {"map": object(), "pbap": None})()
    value.contacts = type(
        "Contacts",
        (),
        {"count": lambda _self: 0, "resolve": lambda _self, raw: raw},
    )()
    value._contacts_refresh_id = None
    value.listener = None
    value.mns_watch = None
    value._refresh_contacts = lambda: calls.append("unexpected-contacts-refresh")
    value.events = type("Events", (), {"message": lambda *_args: None})()
    value.obex_worker = type("Worker", (), {"submit": lambda *_args: None})()
    monkeypatch.setattr(daemon, "MapEventListener", Listener)
    monkeypatch.setattr(daemon, "MnsWatch", _mns_watch(calls))

    value._post_available_sessions_setup()

    assert calls == ["map-listener", "map-listener-start", "mns-watch-start"]
    assert isinstance(value.listener, Listener)
    assert value._contacts_refresh_id is None


def test_map_only_listener_is_replaced_after_obexd_restarts(monkeypatch):
    from blueferry.connectivity import Connectivity
    from blueferry.obex import sessions as sessions_mod
    from blueferry.profile_supervisor import ProfileSupervisor

    jobs, timers, listeners = [], [], []
    value = daemon.Daemon.__new__(daemon.Daemon)
    value.sessions = sessions_mod.SessionManager()
    value.contacts = SimpleNamespace(count=lambda: 0)
    value._contacts_refresh_id = None
    value.listener = None
    value.mns_watch = None
    value.events = SimpleNamespace(message=lambda _event: None)
    value.solicitation = _Solicitation([])
    value.obex_worker = SimpleNamespace(
        submit=lambda operation, **callbacks: jobs.append((operation, callbacks)),
    )

    class Listener:
        def __init__(self, *, sessions, **_kwargs):
            self.path = sessions.map_path
            self.running = False
            listeners.append(self)

        def start(self):
            self.running = True

        def stop(self):
            self.running = False

    def create_session(target):
        if target == "PBAP":
            raise sessions_mod.SessionError("PBAP unavailable")
        return sessions_mod.ObexSession("MAP", "/session0")

    def finish_job():
        operation, callbacks = jobs.pop(0)
        try:
            result = operation()
        except Exception as error:
            callbacks["on_error"](error)
        else:
            callbacks["on_success"](result)

    watch_calls = []
    monkeypatch.setattr(daemon, "MapEventListener", Listener)
    monkeypatch.setattr(daemon, "MnsWatch", _mns_watch(watch_calls))
    monkeypatch.setattr(value.sessions, "start_monitoring", lambda: None)
    monkeypatch.setattr(sessions_mod, "_create_session", create_session)
    profiles = ProfileSupervisor(
        value.sessions, value.obex_worker, Connectivity(),
        on_ready=value._post_available_sessions_setup,
        on_partial_ready=value._post_available_sessions_setup,
        on_lost=value._profiles_lost,
        on_status=lambda: None,
        schedule=lambda _delay, callback: timers.append(callback) or len(timers),
        cancel=lambda _timer: None,
    )
    profiles.start()
    finish_job()
    original = value.listener
    assert original.running
    assert not profiles.ready

    value.sessions._on_name_owner_changed("org.bluez.obex", ":1.2", "")
    assert value.listener is None
    assert not original.running
    finish_job()  # Serialized cleanup of the old sessions.
    timers[-1]()  # Retry on the replacement obexd owner.
    finish_job()

    assert len(listeners) == 2
    assert value.listener is listeners[-1]
    assert value.listener.running
    # A replacement obexd can reuse paths; it still needs a fresh listener.
    assert value.listener.path == original.path
    assert not profiles.ready  # PBAP is still unavailable.
    # The MNS watch follows the listener's session lifecycle.
    assert watch_calls == ["mns-watch-start", "mns-watch-stop", "mns-watch-start"]


def test_automatic_contacts_wait_for_map_but_manual_sync_still_works(monkeypatch):
    from blueferry.backend_operations import BackendDependencies, BackendOperations
    from blueferry.connectivity import Connectivity
    from blueferry.obex.sessions import SessionError
    from blueferry.profile_supervisor import ProfileSupervisor

    jobs, timers, published = [], [], []
    value = daemon.Daemon.__new__(daemon.Daemon)
    value.sessions = SimpleNamespace(map=None, pbap=object(),
                                     set_on_lost=lambda callback: None,
                                     open_all=lambda: None)
    value.contacts = SimpleNamespace(count=lambda: 0)
    value.storage = SimpleNamespace(status=SimpleNamespace(can_write=True))
    value._contacts_refresh_pending = False
    value._contacts_initial_sync_done = False
    value._contacts_storage_generation = 0
    value._contacts_sync_waiters = []
    value._contacts_refresh_deferred = False
    value._contacts_map_wait_id = None
    value._contacts_map_wait_finished = False
    value._contacts_refresh_id = 1
    value.listener = object()
    value._pull_contacts = lambda: 42
    value._contacts_pulled = lambda n: n
    value._emit_status = lambda: None
    handlers = []
    def submit(operation, **callbacks):
        jobs.append(operation)
        handlers.append(callbacks)
    value.obex_worker = SimpleNamespace(submit=submit)
    monkeypatch.setattr(daemon.GLib, 'timeout_add_seconds', lambda *_args: 123)
    monkeypatch.setattr(daemon.GLib, 'source_remove', lambda _: True)
    profiles = ProfileSupervisor(
        value.sessions, value.obex_worker, Connectivity(),
        on_ready=lambda: None, on_lost=lambda _: None, on_status=lambda: None,
        on_partial_ready=value._post_available_sessions_setup,
        schedule=lambda delay, callback: timers.append(callback) or 1,
    )
    profiles._open_failed(0, SessionError('CreateSession(MAP) failed: Forbidden'))
    value._on_storage_changed()  # Wallet unlock must not bypass the same gate.
    assert not jobs
    assert value._contacts_refresh_deferred
    timers.pop()()
    assert jobs == [value.sessions.open_all]
    jobs.clear()
    handlers.clear()

    operations = BackendOperations(value.sessions, BackendDependencies(
        sync_contacts=value._sync_contacts,
    ))
    operations.sync_contacts(published.append, published.append)
    assert jobs == [value._pull_contacts]
    handlers.pop()['on_success'](42)
    assert published == [42]
    jobs.clear()

    value.sessions.map = object()
    value._post_available_sessions_setup()
    assert jobs == [value._pull_contacts]
    assert not value._contacts_refresh_deferred
    value._post_available_sessions_setup()
    assert len(jobs) == 1
