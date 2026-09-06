"""The persisted pairing policy controls long-lived ANCS work."""

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


def _daemon(calls):
    value = daemon.Daemon.__new__(daemon.Daemon)
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
    value._watch_sleep_resume = lambda: calls.append("sleep-watch")
    return value


def _ready_bluetooth(monkeypatch, calls):
    monkeypatch.setattr(daemon, "bond_status", lambda *_args: True)
    monkeypatch.setattr(
        daemon.bluez_setup,
        "prepare",
        lambda: calls.append("solicitation-prepare") or True,
    )


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
        "solicitation-reset",
        "bearers-hold-le",
        ("profiles-reconnect", "bluetoothd restarted", False),
        "bearers-reset",
    ]


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
    value._refresh_contacts = lambda: calls.append("unexpected-contacts-refresh")
    value.events = type("Events", (), {"message": lambda *_args: None})()
    value.obex_worker = type("Worker", (), {"submit": lambda *_args: None})()
    monkeypatch.setattr(daemon, "MapEventListener", Listener)

    value._post_available_sessions_setup()

    assert calls == ["map-listener", "map-listener-start"]
    assert isinstance(value.listener, Listener)
    assert value._contacts_refresh_id is None
