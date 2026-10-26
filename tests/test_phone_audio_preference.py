from __future__ import annotations

from types import SimpleNamespace

import pytest

from blueferry.audio_activity import audio_activity
from blueferry.errors import NotReadyError
from blueferry.phone_audio_preference import PhoneAudioPreference
from blueferry.phone_audio_settings import PhoneAudioSettings
from tests.test_phone_audio_route import Timers

MAC = "AA:BB:CC:DD:EE:FF"


class Route:
    def __init__(self):
        self.current = "phone"
        self.sink_available = True
        self.busy = False
        self.calls = []
        self.probes = 0

    def snapshot(self):
        return {"phone_audio_route": self.current}

    def probe(self):
        self.probes += 1

    def set_route(self, choice, success, failure, *, resume=True):
        self.busy = True
        self.calls.append((choice, resume, success, failure))

    def finish(self, error=None):
        choice, _resume, success, failure = self.calls[-1]
        self.busy = False
        if error is not None:
            failure(error)
        else:
            self.current = choice
            success(choice)


def setup(tmp_path, *, choice="auto", policy_present=False, configure=None):
    settings = PhoneAudioSettings(MAC, tmp_path / "settings.json")
    if choice != "auto":
        settings.set(choice)
    route = Route()
    timers = Timers()
    state = SimpleNamespace(safe=True, ready=True, connected=True, now=0)
    configured = []
    def apply(enabled, done):
        configured.append(enabled)
        try:
            with audio_activity(blocking=False):
                result = "active" if enabled else "desktop"
        except OSError as error:
            result = error
        done(result)
    preference = PhoneAudioPreference(
        route, settings, configure=configure or apply,
        connected=lambda: state.connected, safe=lambda: state.safe,
        automatic_ready=lambda: state.ready,
        schedule=timers.schedule, cancel=timers.cancel,
        on_changed=lambda: None, clock=lambda: state.now, policy_present=policy_present,
    )
    preference.start()
    return preference, route, settings, state, timers, configured


def test_default_does_nothing_until_the_user_makes_a_choice(tmp_path):
    p, route, settings, _state, timers, configured = setup(tmp_path)
    p.connection_changed()
    timers.fire()
    assert settings.choice == "auto" and configured == [] and route.calls == []
    assert timers.pending == {}


def test_explicit_pc_choice_is_saved_after_the_profile_operation_succeeds(tmp_path):
    p, route, settings, _state, timers, configured = setup(tmp_path)
    replies, failures = [], []
    p.set_route("pc", replies.append, failures.append)
    timers.fire()  # install/reload the rule, then observe endpoints
    assert configured == [True] and route.probes == 1 and route.calls == []
    timers.fire()
    assert route.calls[-1][:2] == ("pc", True)
    assert settings.choice == "auto" and replies == []
    route.finish()
    assert settings.choice == "pc" and replies == ["pc"] and not failures
    p.poke()
    timers.fire()
    assert len(route.calls) == 1
    p.set_route("phone", replies.append, failures.append)
    timers.fire()
    route.finish()
    assert settings.choice == "phone" and configured == [True]  # no second reload


def test_first_use_waits_for_registered_audio_sink_after_reload(tmp_path):
    p, route, _settings, _state, timers, _configured = setup(tmp_path)
    route.sink_available = False
    p.set_route("pc", lambda _r: None, pytest.fail)
    timers.fire()
    timers.fire()
    assert not route.calls and route.probes >= 2
    route.sink_available = True
    timers.fire()
    assert route.calls[-1][0] == "pc"
    route.finish()


def test_pairing_blocks_policy_reload_and_audio_calls(tmp_path):
    _p, route, _settings, _state, timers, configured = setup(tmp_path, choice="phone")
    route.current = "pc"
    with audio_activity():
        timers.fire()
        timers.fire()
        assert route.calls == []
    timers.fire()
    with audio_activity():
        timers.fire()
        assert route.calls == []
    timers.fire()
    assert route.calls[-1][:2] == ("phone", True)
    route.finish()
    assert configured


def test_audio_guard_is_held_until_dbus_reply_even_if_transport_changes_early(tmp_path):
    p, route, _settings, _state, timers, _configured = setup(tmp_path)
    p.set_route("pc", lambda _r: None, pytest.fail)
    timers.fire()
    timers.fire()
    route.current = "pc"
    with pytest.raises(BlockingIOError), audio_activity(blocking=False):
        pass
    route.finish()
    with audio_activity(blocking=False):
        pass


def test_automatic_restore_waits_for_setup_and_uses_normal_handback(tmp_path):
    _p, route, _settings, state, timers, configured = setup(tmp_path, choice="phone")
    route.current = "pc"
    state.ready = False
    timers.fire()
    timers.fire()
    assert not route.calls and not configured
    state.ready = True
    timers.fire()
    timers.fire()
    assert route.calls[-1][:2] == ("phone", True)
    route.finish()


def test_only_one_automatic_attempt_per_connection_then_honor_manual_pc(tmp_path):
    p, route, _settings, state, timers, configured = setup(tmp_path, choice="phone")
    p.connection_changed()
    timers.fire()
    timers.fire()
    assert not route.calls  # already on phone; wait for the incoming stream
    route.current = "pc"
    p.poke()
    timers.fire()
    route.finish()
    route.current = "pc"  # another audio manager immediately reopens it
    p.poke()
    timers.fire()
    assert len(route.calls) == 1
    state.connected = False
    p.connection_changed()
    state.connected = True
    p.connection_changed()
    timers.fire()
    assert len(route.calls) == 2
    route.finish()
    p.set_route("pc", lambda _r: None, pytest.fail)
    timers.fire()
    route.finish()
    state.connected = False
    p.connection_changed()
    route.current = "phone"
    state.connected = True
    p.connection_changed()
    timers.fire()
    assert route.calls[-1][:2] == ("pc", True)
    route.finish()
    assert configured == [True]


def test_failed_profile_operation_does_not_save_and_does_not_loop(tmp_path):
    p, route, settings, _state, timers, _configured = setup(tmp_path, choice="phone")
    timers.fire()
    failures = []
    p.set_route("pc", pytest.fail, failures.append)
    timers.fire()
    route.finish(NotReadyError("busy"))
    assert settings.choice == "phone" and len(failures) == 1
    for _ in range(3):
        p.poke()
        timers.fire()
    assert len(route.calls) == 1


def test_auto_removes_the_policy_and_does_not_send_bluetooth_operations(tmp_path):
    p, route, settings, _state, timers, configured = setup(tmp_path, choice="phone")
    timers.fire()
    replies = []
    p.set_route("auto", replies.append, pytest.fail)
    timers.fire()
    timers.fire()
    assert configured == [True, False]
    assert settings.choice == "auto" and replies == ["auto"]
    assert route.calls == []


def test_stale_fragment_is_removed_when_the_selected_phone_has_no_preference(tmp_path):
    _p, route, settings, _state, timers, configured = setup(tmp_path, policy_present=True)
    timers.fire()
    assert configured == [False] and settings.choice == "auto" and not route.calls


def test_stop_fails_pending_request_and_ignores_late_profile_reply(tmp_path):
    p, route, settings, _state, timers, _configured = setup(tmp_path)
    errors, replies = [], []
    p.set_route("pc", replies.append, errors.append)
    timers.fire()
    timers.fire()
    p.stop()
    route.finish()
    assert not replies and len(errors) == 1 and settings.choice == "auto"
    assert not timers.pending
    with audio_activity(blocking=False):
        pass


def test_backend_stop_ignores_late_reload_callback(tmp_path):
    held = []
    p, route, _settings, _state, timers, _configured = setup(
        tmp_path, choice="phone", configure=lambda enabled, done: held.append(done),
    )
    timers.fire()
    p.stop()
    held[0]("active")
    assert not route.calls and route.probes == 0 and not timers.pending


def test_busy_runtime_times_out_manual_request_without_saving(tmp_path):
    p, route, settings, state, timers, configured = setup(tmp_path)
    state.safe = False
    errors = []
    p.set_route("pc", pytest.fail, errors.append)
    timers.fire()
    state.now = 26
    timers.fire()
    assert len(errors) == 1 and settings.choice == "auto"
    assert not configured and not route.calls


def test_failed_reload_reports_failure_and_does_not_change_saved_choice(tmp_path):
    attempts = []
    def configure(enabled, done):
        attempts.append(enabled)
        done(OSError("reload failed"))
    p, route, settings, _state, timers, _configured = setup(tmp_path, configure=configure)
    errors = []
    p.set_route("pc", pytest.fail, errors.append)
    timers.fire()
    for _ in range(3):
        p.poke()
        timers.fire()
    assert len(errors) == 1 and settings.choice == "auto"
    assert len(attempts) == 1 and not route.calls


def test_daemon_reloads_off_mainloop_and_returns_result_on_idle(make_daemon, monkeypatch):
    from blueferry import daemon
    instance = make_daemon()
    worker, idle, applied, results = [], [], [], []
    monkeypatch.setattr(daemon, "_in_background", lambda work, _name: worker.append(work))
    instance._idle_add = idle.append
    instance.audio_autoconnect.configure = lambda mac: applied.append(mac) or "active"
    instance._configure_phone_audio_policy(True, results.append)
    assert not applied and not results
    worker.pop()()
    assert applied == [daemon.config.IPHONE_MAC] and not results
    idle.pop()()
    assert results == ["active"]


def test_daemon_policy_worker_defers_to_pairing(make_daemon, monkeypatch):
    from blueferry import daemon
    instance = make_daemon()
    idle, results = [], []
    instance._idle_add = idle.append
    instance.audio_autoconnect.configure = lambda _mac: pytest.fail("pairing is active")
    monkeypatch.setattr(daemon, "_in_background", lambda work, _name: work())
    with audio_activity():
        instance._configure_phone_audio_policy(True, results.append)
    idle.pop()()
    assert len(results) == 1 and isinstance(results[0], BlockingIOError)


def test_unknown_bearer_observation_does_not_reset_the_one_attempt_budget(tmp_path):
    p, route, _settings, state, timers, _configured = setup(tmp_path, choice="phone")
    route.current = "pc"
    timers.fire()
    timers.fire()
    route.finish()
    route.current = "pc"
    state.connected = None
    p.connection_changed()
    state.connected = True
    p.connection_changed()
    timers.fire()
    assert len(route.calls) == 1


def test_remembering_the_current_route_does_not_send_a_redundant_profile_request(tmp_path):
    p, route, settings, _state, timers, _configured = setup(tmp_path)
    replies = []
    p.set_route("phone", replies.append, pytest.fail)
    assert p.snapshot()["phone_audio_pending"] == "phone"
    timers.fire()
    timers.fire()
    assert replies == ["phone"] and settings.choice == "phone"
    assert not route.calls and p.snapshot()["phone_audio_pending"] == ""


def test_clearing_the_preference_works_while_the_phone_is_unavailable(tmp_path):
    p, route, settings, state, timers, configured = setup(tmp_path, choice="phone")
    state.connected = False
    state.ready = False
    route.current = "unavailable"
    replies = []
    p.set_route("auto", replies.append, pytest.fail)
    timers.fire()
    timers.fire()
    assert replies == ["auto"] and settings.choice == "auto"
    assert configured == [False] and not route.calls
