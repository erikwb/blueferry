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
    value.notification_policy = type("Policy", (), {"value": "messages"})()
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
    value = _daemon(calls)
    _ready_bluetooth(monkeypatch, calls)
    monkeypatch.setattr(daemon.config, "ANCS_ENABLED", True)

    class Ancs:
        def __init__(self, *_args, **kwargs):
            calls.append("ancs-client")
            calls.append(("previously-authorized", kwargs["previously_authorized"]))

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
