"""Proximity lock: grace state machine and lock dispatch, with fakes only."""
from __future__ import annotations

import json

import pytest

from blueferry import proximity_lock as pl


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeTimers:
    """Deterministic GLib timeout replacement driven by FakeClock."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.pending: dict[int, tuple[float, object]] = {}
        self.next_id = 1

    def schedule(self, seconds, callback) -> int:
        timer = self.next_id
        self.next_id += 1
        self.pending[timer] = (self.clock.now + seconds, callback)
        return timer

    def cancel(self, timer) -> None:
        self.pending.pop(timer, None)

    def advance(self, seconds: float) -> None:
        target = self.clock.now + seconds
        while True:
            due = [
                (deadline, timer)
                for timer, (deadline, _callback) in self.pending.items()
                if deadline <= target
            ]
            if not due:
                break
            deadline, timer = min(due)
            self.clock.now = deadline
            _deadline, callback = self.pending.pop(timer)
            if callback():
                raise AssertionError("proximity timers are one-shot")
        self.clock.now = target


class FakeLocker:
    def __init__(self) -> None:
        self.calls = 0
        self.pending: list = []
        self.immediate: str | None = pl.RESULT_SCREENSAVER

    def lock(self, done) -> None:
        self.calls += 1
        if self.immediate is None:
            self.pending.append(done)
        else:
            done(self.immediate)

    def plan(self) -> list[str]:
        return ["fake"]


class Harness:
    def __init__(self, *, enabled: bool = True, grace: int = 60) -> None:
        self.clock = FakeClock()
        self.timers = FakeTimers(self.clock)
        self.locker = FakeLocker()
        self.presence: bool | None = None
        self.status_events = 0
        self.lock = pl.ProximityLock(
            enabled=enabled,
            grace_sec=grace,
            read_presence=lambda: self.presence,
            locker=self.locker,
            on_status=self._status,
            schedule=self.timers.schedule,
            cancel=self.timers.cancel,
            clock=self.clock,
        )

    def _status(self) -> None:
        self.status_events += 1

    def see(self, presence: bool | None) -> None:
        self.presence = presence
        self.lock.bearer_changed()


# ---- state machine ------------------------------------------------------


def test_disabled_is_inert_even_when_the_phone_leaves() -> None:
    harness = Harness(enabled=False)
    harness.see(True)
    harness.see(False)
    harness.timers.advance(3600)

    assert harness.lock.state == pl.STATE_DISABLED
    assert harness.locker.calls == 0
    assert harness.timers.pending == {}
    assert harness.lock.snapshot()["proximity_lock_enabled"] is False


def test_locks_once_after_continuous_absence_for_the_grace_period() -> None:
    harness = Harness(grace=60)
    harness.see(True)
    assert harness.lock.state == pl.STATE_ARMED

    harness.see(False)
    assert harness.lock.state == pl.STATE_GRACE
    harness.timers.advance(59)
    assert harness.locker.calls == 0
    assert harness.lock.snapshot()["proximity_lock_remaining_sec"] == 1

    harness.timers.advance(1)
    assert harness.locker.calls == 1
    assert harness.lock.state == pl.STATE_LOCKED
    assert harness.lock.snapshot()["proximity_lock_last_result"] == "screensaver"


def test_does_not_relock_until_the_phone_has_been_seen_again() -> None:
    harness = Harness(grace=30)
    harness.see(True)
    harness.see(False)
    harness.timers.advance(30)
    assert harness.locker.calls == 1

    # Still away, repeated bearer polls and hours pass: no second lock.
    for _ in range(10):
        harness.see(False)
        harness.timers.advance(600)
    assert harness.locker.calls == 1
    assert harness.lock.state == pl.STATE_LOCKED

    # Back, then away again: one new lock.
    harness.see(True)
    assert harness.lock.state == pl.STATE_ARMED
    harness.see(False)
    harness.timers.advance(30)
    assert harness.locker.calls == 2


def test_brief_drops_inside_the_grace_period_never_lock() -> None:
    harness = Harness(grace=60)
    harness.see(True)
    for _ in range(20):
        harness.see(False)
        harness.timers.advance(45)
        harness.see(True)
        harness.timers.advance(5)

    assert harness.locker.calls == 0
    assert harness.lock.state == pl.STATE_ARMED
    assert harness.timers.pending == {}


def test_a_return_restarts_the_full_grace_period() -> None:
    harness = Harness(grace=60)
    harness.see(True)
    harness.see(False)
    harness.timers.advance(50)
    harness.see(True)
    harness.see(False)
    harness.timers.advance(50)
    assert harness.locker.calls == 0
    harness.timers.advance(10)
    assert harness.locker.calls == 1


def test_never_locks_when_the_phone_was_not_seen_since_startup() -> None:
    harness = Harness(grace=10)
    harness.see(False)
    harness.timers.advance(3600)

    assert harness.lock.state == pl.STATE_IDLE
    assert harness.locker.calls == 0


def test_unknown_bearer_state_neither_arms_nor_starts_grace() -> None:
    harness = Harness(grace=10)
    harness.see(None)
    assert harness.lock.state == pl.STATE_IDLE
    harness.see(True)
    harness.see(None)
    assert harness.lock.state == pl.STATE_ARMED
    harness.timers.advance(60)
    assert harness.locker.calls == 0


def test_unknown_at_the_deadline_does_not_lock() -> None:
    harness = Harness(grace=10)
    harness.see(True)
    harness.see(False)
    harness.presence = None  # bluetoothd stopped answering, no callback
    harness.timers.advance(10)

    assert harness.locker.calls == 0
    assert harness.lock.state == pl.STATE_IDLE


def test_return_without_status_callback_is_rechecked_at_the_deadline() -> None:
    harness = Harness(grace=10)
    harness.see(True)
    harness.see(False)
    harness.presence = True
    harness.timers.advance(10)

    assert harness.locker.calls == 0
    assert harness.lock.state == pl.STATE_ARMED


@pytest.mark.parametrize("reason", [
    pl.INHIBIT_FORGOTTEN,
    pl.INHIBIT_DISCOVERING,
    pl.INHIBIT_ADAPTER_OFF,
    pl.INHIBIT_RECOVERY,
])
def test_inhibitors_cancel_grace_and_require_fresh_presence(reason) -> None:
    harness = Harness(grace=30)
    harness.see(True)
    harness.see(False)
    harness.timers.advance(20)
    harness.lock.inhibit(reason)
    harness.timers.advance(600)
    assert harness.locker.calls == 0
    assert harness.lock.state == pl.STATE_IDLE
    assert harness.lock.snapshot()["proximity_lock_inhibited"] == reason

    # Ending the inhibitor while the phone is still away does not re-arm.
    harness.lock.inhibit(reason, False)
    harness.timers.advance(600)
    assert harness.locker.calls == 0
    assert harness.lock.state == pl.STATE_IDLE

    harness.see(True)
    assert harness.lock.state == pl.STATE_ARMED


def test_inhibitor_end_rearms_after_one_poll_when_the_phone_stayed() -> None:
    harness = Harness(grace=30)
    harness.see(True)
    harness.lock.inhibit(pl.INHIBIT_DISCOVERING)
    harness.lock.inhibit(pl.INHIBIT_DISCOVERING, False)
    # The cache may predate the inhibitor's end; do not trust it yet.
    assert harness.lock.state == pl.STATE_IDLE
    harness.timers.advance(pl.PRESENCE_SETTLE_SEC)
    assert harness.lock.state == pl.STATE_ARMED


def test_a_fresh_transition_arms_before_the_settle_poll() -> None:
    harness = Harness(grace=30)
    harness.see(False)
    harness.lock.inhibit(pl.INHIBIT_DISCOVERING)
    harness.lock.inhibit(pl.INHIBIT_DISCOVERING, False)
    harness.see(True)
    assert harness.lock.state == pl.STATE_ARMED
    assert harness.timers.pending == {}


def test_stale_present_cache_at_resume_cannot_arm() -> None:
    harness = Harness(grace=10)
    harness.see(True)
    harness.lock.suspending()
    harness.presence = True  # cache still says connected from before suspend
    harness.lock.resumed()
    assert harness.lock.state == pl.STATE_IDLE
    # The first post-resume poll finds the link gone; the supervisor reports
    # that transition while the lock is still unarmed.
    harness.see(False)
    harness.timers.advance(3600)
    assert harness.lock.state == pl.STATE_IDLE
    assert harness.locker.calls == 0


def test_overlapping_inhibitors_hold_until_all_end() -> None:
    harness = Harness(grace=30)
    harness.see(True)
    harness.lock.inhibit(pl.INHIBIT_ADAPTER_OFF)
    harness.lock.inhibit(pl.INHIBIT_RECOVERY)
    harness.lock.inhibit(pl.INHIBIT_ADAPTER_OFF, False)
    harness.see(True)
    assert harness.lock.state == pl.STATE_IDLE
    harness.lock.inhibit(pl.INHIBIT_RECOVERY, False)
    harness.timers.advance(pl.PRESENCE_SETTLE_SEC)
    assert harness.lock.state == pl.STATE_ARMED


def test_suspend_cancels_grace_and_resume_needs_a_fresh_link() -> None:
    harness = Harness(grace=60)
    harness.see(True)
    harness.see(False)
    harness.timers.advance(30)

    harness.lock.suspending()
    harness.timers.advance(3600)
    assert harness.locker.calls == 0

    # The daemon re-reads bearers before calling resumed(); the phone has not
    # reconnected yet after resume, which must not count as walking away.
    harness.presence = False
    harness.lock.resumed()
    harness.timers.advance(3600)
    assert harness.locker.calls == 0
    assert harness.lock.state == pl.STATE_IDLE

    harness.see(True)
    harness.see(False)
    harness.timers.advance(60)
    assert harness.locker.calls == 1


def test_suspend_while_armed_and_resume_with_phone_present_rearms() -> None:
    harness = Harness(grace=60)
    harness.see(True)
    harness.lock.suspending()
    harness.presence = True
    harness.lock.resumed()
    harness.timers.advance(pl.PRESENCE_SETTLE_SEC)
    assert harness.lock.state == pl.STATE_ARMED


def test_disabling_during_grace_cancels_and_enabling_needs_the_phone() -> None:
    harness = Harness(grace=60)
    harness.see(True)
    harness.see(False)
    harness.lock.configure(False, 60)
    assert harness.timers.pending == {}
    harness.timers.advance(600)
    assert harness.locker.calls == 0
    assert harness.lock.state == pl.STATE_DISABLED

    harness.presence = False
    harness.lock.configure(True, 60)
    harness.timers.advance(600)
    assert harness.locker.calls == 0
    assert harness.lock.state == pl.STATE_IDLE

    harness.see(True)
    assert harness.lock.state == pl.STATE_ARMED


def test_enabling_while_connected_arms_after_one_poll() -> None:
    harness = Harness(enabled=False)
    harness.presence = True
    harness.lock.configure(True, 60)
    assert harness.lock.state == pl.STATE_IDLE
    harness.timers.advance(pl.PRESENCE_SETTLE_SEC)
    assert harness.lock.state == pl.STATE_ARMED


def test_shortening_grace_during_grace_uses_the_original_absence_start() -> None:
    harness = Harness(grace=120)
    harness.see(True)
    harness.see(False)
    harness.timers.advance(40)
    harness.lock.configure(True, 30)
    # Already away longer than the new period: lock on the next tick.
    harness.timers.advance(1)
    assert harness.locker.calls == 1


def test_stop_cancels_pending_grace_and_ignores_late_results() -> None:
    harness = Harness(grace=10)
    harness.locker.immediate = None
    harness.see(True)
    harness.see(False)
    harness.timers.advance(10)
    assert harness.locker.calls == 1
    harness.lock.stop()
    harness.locker.pending[0](pl.RESULT_SCREENSAVER)
    assert harness.lock.snapshot()["proximity_lock_last_result"] == ""

    harness.see(True)
    harness.see(False)
    harness.timers.advance(600)
    assert harness.locker.calls == 1


def test_failed_dispatch_is_reported_and_not_retried() -> None:
    harness = Harness(grace=10)
    harness.locker.immediate = pl.RESULT_FAILED
    harness.see(True)
    harness.see(False)
    harness.timers.advance(10)
    harness.timers.advance(3600)

    assert harness.locker.calls == 1
    snapshot = harness.lock.snapshot()
    assert snapshot["proximity_lock"] == pl.STATE_LOCKED
    assert snapshot["proximity_lock_last_result"] == pl.RESULT_FAILED


def test_state_changes_emit_content_free_status() -> None:
    harness = Harness(grace=10)
    before = harness.status_events
    harness.see(True)
    harness.see(False)
    harness.timers.advance(10)
    assert harness.status_events >= before + 3
    snapshot = harness.lock.snapshot()
    assert set(snapshot) == {
        "proximity_lock",
        "proximity_lock_enabled",
        "proximity_lock_grace_sec",
        "proximity_lock_remaining_sec",
        "proximity_lock_inhibited",
        "proximity_lock_last_result",
    }
    json.dumps(snapshot)


@pytest.mark.parametrize(("bredr", "le", "expected"), [
    (True, None, True),
    (False, True, True),
    (None, True, True),
    (False, False, False),
    (False, None, False),
    (None, False, None),
    (None, None, None),
])
def test_presence_from_bearers(bredr, le, expected) -> None:
    assert pl.presence_from_bearers(bredr, le) is expected


@pytest.mark.parametrize(("value", "expected"), [
    (5, pl.MIN_GRACE_SEC),
    (60, 60),
    (10**6, pl.MAX_GRACE_SEC),
    ("abc", pl.DEFAULT_GRACE_SEC),
    (True, pl.DEFAULT_GRACE_SEC),
    (None, pl.DEFAULT_GRACE_SEC),
])
def test_clamp_grace(value, expected) -> None:
    assert pl.clamp_grace(value) == expected


# ---- settings -----------------------------------------------------------


def test_settings_default_off_and_persist_owner_only(isolated_state) -> None:
    from blueferry import config

    settings = pl.ProximityLockSettings(default_enabled=False, default_grace=60)
    assert settings.enabled is False
    assert settings.grace_sec == 60

    assert settings.set(True, 90) == (True, 90)
    reloaded = pl.ProximityLockSettings(default_enabled=False, default_grace=60)
    assert (reloaded.enabled, reloaded.grace_sec) == (True, 90)
    assert config.SETTINGS_JSON.stat().st_mode & 0o777 == 0o600


def test_settings_env_default_used_until_a_value_is_saved(isolated_state) -> None:
    settings = pl.ProximityLockSettings(default_enabled=True, default_grace=5)
    assert settings.enabled is True
    assert settings.grace_sec == pl.MIN_GRACE_SEC

    settings.set(False, 120)
    reloaded = pl.ProximityLockSettings(default_enabled=True, default_grace=45)
    assert (reloaded.enabled, reloaded.grace_sec) == (False, 120)


def test_saved_preference_overriding_the_environment_is_logged_once(
    isolated_state, monkeypatch, caplog,
) -> None:
    from blueferry import config

    pl.ProximityLockSettings(default_enabled=False).set(False, 120)
    monkeypatch.setenv("BLUEFERRY_PROXIMITY_LOCK", "true")
    monkeypatch.setenv("BLUEFERRY_PROXIMITY_LOCK_GRACE_SEC", "30")
    monkeypatch.setattr(config, "PROXIMITY_LOCK", True)
    monkeypatch.setattr(config, "PROXIMITY_LOCK_GRACE_SEC", 30)
    with caplog.at_level("INFO", logger="blueferry.proximity_lock"):
        settings = pl.ProximityLockSettings()
    assert (settings.enabled, settings.grace_sec) == (False, 120)
    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 2
    assert "BLUEFERRY_PROXIMITY_LOCK is ignored" in messages[0]
    assert "BLUEFERRY_PROXIMITY_LOCK_GRACE_SEC is ignored" in messages[1]


def test_matching_or_unset_environment_is_not_logged(
    isolated_state, monkeypatch, caplog,
) -> None:
    from blueferry import config

    pl.ProximityLockSettings(default_enabled=False).set(True, 60)
    monkeypatch.delenv("BLUEFERRY_PROXIMITY_LOCK", raising=False)
    monkeypatch.setenv("BLUEFERRY_PROXIMITY_LOCK_GRACE_SEC", "60")
    monkeypatch.setattr(config, "PROXIMITY_LOCK_GRACE_SEC", 60)
    with caplog.at_level("INFO", logger="blueferry.proximity_lock"):
        pl.ProximityLockSettings()
    assert caplog.records == []


@pytest.mark.parametrize(("enabled", "grace"), [
    ("yes", 60), (True, 9), (True, 3601), (True, 60.0), (True, True),
])
def test_settings_reject_invalid_values(isolated_state, enabled, grace) -> None:
    settings = pl.ProximityLockSettings(default_enabled=False)
    with pytest.raises(ValueError):
        settings.set(enabled, grace)
    assert settings.enabled is False


def test_config_keys_are_accepted_from_local_env() -> None:
    from blueferry import config

    assert "BLUEFERRY_PROXIMITY_LOCK" in config.LOCAL_ENV_KEYS
    assert "BLUEFERRY_PROXIMITY_LOCK_GRACE_SEC" in config.LOCAL_ENV_KEYS


def test_config_default_is_off_without_environment(tmp_path) -> None:
    """Import config in a clean process: no env, no local.env."""
    import os
    import subprocess  # nosec B404 - fixed argv, inert child
    import sys
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "src"
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(source),
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }
    output = subprocess.run(  # nosec B603
        [sys.executable, "-c",
         "from blueferry import config;"
         "print(config.PROXIMITY_LOCK, config.PROXIMITY_LOCK_GRACE_SEC)"],
        env=env, capture_output=True, text=True, check=True, timeout=60,
    ).stdout.split()
    assert output == ["False", "60"]


def test_status_is_emitted_only_when_the_snapshot_changes() -> None:
    harness = Harness(grace=30)
    assert harness.status_events == 0
    harness.see(None)
    harness.see(False)  # not armed yet: nothing changes
    assert harness.status_events == 0
    harness.see(True)
    assert harness.status_events == 1
    harness.see(True)
    harness.lock.configure(True, 30)
    harness.lock.inhibit(pl.INHIBIT_ADAPTER_OFF, False)  # not active
    assert harness.status_events == 1
    harness.lock.inhibit(pl.INHIBIT_ADAPTER_OFF)
    assert harness.status_events == 2
    harness.lock.inhibit(pl.INHIBIT_ADAPTER_OFF)
    assert harness.status_events == 2
    harness.lock.inhibit(pl.INHIBIT_ADAPTER_OFF, False)
    assert harness.status_events == 3
    harness.timers.advance(pl.PRESENCE_SETTLE_SEC)  # re-armed: one event
    assert harness.status_events == 4
    harness.see(False)
    harness.timers.advance(10)  # countdown alone is not a change
    assert harness.status_events == 5


# ---- lock dispatch --------------------------------------------------------


class FakeDBusError(Exception):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self._name = name

    def get_dbus_name(self) -> str:
        return self._name


class ScriptedBus:
    """Answer (bus, path, method) with a reply value or an error name."""

    def __init__(self, script: dict) -> None:
        self.script = script
        self.calls: list[tuple] = []
        self.timeouts: list[float] = []

    def __call__(self, bus, service, path, interface, method, signature, args, reply, error,
                 *, timeout):
        self.calls.append((bus, service, path, interface, method, signature, args))
        self.timeouts.append(timeout)
        outcome = self.script.get((bus, path, method), FakeDBusError(
            "org.freedesktop.DBus.Error.ServiceUnknown"
        ))
        if isinstance(outcome, Exception):
            error(outcome)
        elif outcome is None:
            reply()
        else:
            reply(outcome)


def _lock(bus: ScriptedBus, environ=None) -> list[str]:
    results: list[str] = []
    pl.DesktopLocker(bus, pid=lambda: 4242, environ=environ or {}).lock(results.append)
    return results


SCREENSAVER = ("session", "/org/freedesktop/ScreenSaver", "Lock")
BY_PID = ("system", "/org/freedesktop/login1", "GetSessionByPID")
BY_ID = ("system", "/org/freedesktop/login1", "GetSession")


def test_screensaver_lock_is_preferred_and_logind_untouched() -> None:
    bus = ScriptedBus({SCREENSAVER: None})
    assert _lock(bus) == [pl.RESULT_SCREENSAVER]
    assert [call[0] for call in bus.calls] == ["session"]
    assert bus.calls[0][1:5] == (
        "org.freedesktop.ScreenSaver",
        "/org/freedesktop/ScreenSaver",
        "org.freedesktop.ScreenSaver",
        "Lock",
    )


def test_screensaver_gets_a_long_timeout_and_logind_a_short_one() -> None:
    session = "/org/freedesktop/login1/session/c2"
    bus = ScriptedBus({BY_PID: session, ("system", session, "Lock"): None})
    _lock(bus)
    assert bus.timeouts[0] == pl.SCREENSAVER_LOCK_TIMEOUT_SEC >= 30
    assert all(value == pl.LOCK_CALL_TIMEOUT_SEC for value in bus.timeouts[1:])


@pytest.mark.parametrize("name", [
    "org.freedesktop.DBus.Error.NoReply",
    "org.freedesktop.DBus.Error.Timeout",
])
def test_slow_screensaver_reply_counts_as_requested_without_fallback(name) -> None:
    # kscreenlocker replies only once its greeter is up, which can exceed
    # the call timeout under load. The request is already being handled.
    bus = ScriptedBus({SCREENSAVER: FakeDBusError(name)})
    assert _lock(bus) == [pl.RESULT_SCREENSAVER_REQUESTED]
    assert [call[0] for call in bus.calls] == ["session"]


def test_falls_back_to_the_own_logind_session_by_pid() -> None:
    session = "/org/freedesktop/login1/session/c2"
    bus = ScriptedBus({
        BY_PID: session,
        ("system", session, "Lock"): None,
    })
    assert _lock(bus) == [pl.RESULT_LOGIN1]
    methods = [(call[0], call[4]) for call in bus.calls]
    assert methods == [
        ("session", "Lock"),
        ("system", "GetSessionByPID"),
        ("system", "Lock"),
    ]
    assert bus.calls[1][5:] == ("u", (4242,))
    assert bus.calls[2][2:4] == (session, "org.freedesktop.login1.Session")


def test_falls_back_to_xdg_session_id_then_auto() -> None:
    session = "/org/freedesktop/login1/session/_31"
    bus = ScriptedBus({
        BY_ID: session,
        ("system", session, "Lock"): None,
    })
    assert _lock(bus, {"XDG_SESSION_ID": "1"}) == [pl.RESULT_LOGIN1]
    assert bus.calls[2][4:] == ("GetSession", "s", ("1",))

    auto = "/org/freedesktop/login1/session/auto"
    bus = ScriptedBus({("system", auto, "Lock"): None})
    assert _lock(bus, {"XDG_SESSION_ID": "bad id; rm"}) == [pl.RESULT_LOGIN1]
    assert [call[4] for call in bus.calls] == ["Lock", "GetSessionByPID", "Lock"]
    assert bus.calls[-1][2] == auto


def test_all_paths_failing_reports_failure_once() -> None:
    bus = ScriptedBus({})
    assert _lock(bus, {"XDG_SESSION_ID": "3"}) == [pl.RESULT_FAILED]
    assert [call[4] for call in bus.calls] == [
        "Lock", "GetSessionByPID", "GetSession", "Lock",
    ]


def test_unexpected_session_path_is_not_called() -> None:
    auto = "/org/freedesktop/login1/session/auto"
    bus = ScriptedBus({
        BY_PID: "/org/evil/path",
        ("system", auto, "Lock"): None,
    })
    assert _lock(bus) == [pl.RESULT_LOGIN1]
    assert all(call[2] != "/org/evil/path" for call in bus.calls)


def test_synchronous_connection_failure_falls_through() -> None:
    calls = []

    def broken(bus, *args, timeout):
        calls.append(bus)
        if bus == "session":
            raise RuntimeError("no session bus")
        args[-1](FakeDBusError("org.freedesktop.DBus.Error.AccessDenied"))

    results: list[str] = []
    pl.DesktopLocker(broken, pid=lambda: 1, environ={}).lock(results.append)
    assert results == [pl.RESULT_FAILED]
    assert calls == ["session", "system", "system"]


def test_plan_describes_the_dispatch_order_without_calling() -> None:
    bus = ScriptedBus({})
    plan = pl.DesktopLocker(bus, environ={"XDG_SESSION_ID": "2"}).plan()
    assert "ScreenSaver" in plan[0]
    assert any("XDG_SESSION_ID" in step for step in plan)
    assert bus.calls == []


# ---- desktop-side disconnect (BlueZ Device1.Disconnected reason Local) ----


def test_local_disconnect_pauses_until_the_next_connection() -> None:
    harness = Harness(grace=30)
    harness.see(True)
    harness.see(False)
    harness.lock.local_disconnect()  # arrives after the poll saw the loss
    harness.timers.advance(3600)
    assert harness.locker.calls == 0
    assert harness.lock.snapshot()["proximity_lock_inhibited"] == "local-disconnect"

    harness.see(True)  # reconnect ends the pause and is fresh presence
    assert harness.lock.state == pl.STATE_ARMED
    assert harness.lock.snapshot()["proximity_lock_inhibited"] == ""
    harness.see(False)
    harness.timers.advance(30)
    assert harness.locker.calls == 1


def test_local_disconnect_before_the_poll_ignores_the_loss() -> None:
    harness = Harness(grace=30)
    harness.see(True)
    harness.lock.local_disconnect()
    harness.see(False)
    harness.timers.advance(3600)
    assert harness.locker.calls == 0


def test_local_disconnect_does_not_override_other_inhibitors() -> None:
    harness = Harness(grace=30)
    harness.see(True)
    harness.lock.inhibit(pl.INHIBIT_ADAPTER_OFF)
    harness.lock.local_disconnect()
    harness.see(True)
    assert harness.lock.state == pl.STATE_IDLE
    assert harness.lock.snapshot()["proximity_lock_inhibited"] == "adapter-off"
