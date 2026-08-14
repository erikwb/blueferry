"""The persisted pairing policy controls long-lived ANCS work."""

from blueferry import daemon


class _Bearer:
    def __init__(self, calls):
        self.calls = calls

    def start(self):
        self.calls.append("bearers-start")


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


def _daemon(calls):
    value = daemon.Daemon.__new__(daemon.Daemon)
    value.bearers = _Bearer(calls)
    value.events = _Events(calls)
    value.profiles = _Profiles(calls)
    value.notification_policy = type("Policy", (), {"value": "messages"})()
    value.ancs = None
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
        "solicitation-prepare",
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
        def __init__(self, *_args, **_kwargs):
            calls.append("ancs-client")

        def start(self):
            calls.append("ancs-start")

        def stop(self):
            calls.append("ancs-stop")

    monkeypatch.setattr(daemon, "AncsClient", Ancs)

    value._initialize_bluetooth()

    assert "ancs-client" in calls
    assert "ancs-start" in calls
    assert value.ancs is not None
