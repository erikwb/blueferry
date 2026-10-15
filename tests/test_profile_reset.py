"""Profile self-healing: detection, guards, rate limit and the reset itself.

Everything runs against fakes: a manual clock and timer queue, an injected
connect/disconnect pair for the bearer supervisor and no bus at all.
"""
from __future__ import annotations

import json
import logging

import dbus
import pytest

from blueferry import config, profile_reset
from blueferry.bearer_supervisor import PROFILE_RESET_SETTLE_SECONDS, BearerSupervisor
from blueferry.profile_reset import (
    AUTO_RESET_INTERVAL_SECONDS,
    REASON_HFP,
    REASON_MANUAL,
    ProfileResetController,
)
from blueferry.reconnect_view import reconnect_view, result_text

TIMEDOUT = "org.ofono.Error.Timedout"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Harness:
    def __init__(self, *, enabled=True, result="started") -> None:
        self.clock = Clock()
        self.resets: list[str] = []
        self.result = result
        self.peer = True
        self.blocked = False
        self.call = False
        self.recovery = False
        self.changes = 0
        self.subject = ProfileResetController(
            enabled=enabled,
            reset=self._reset,
            peer_connected=lambda: self.peer,
            bluez_blocked=lambda: self.blocked,
            call_active=lambda: self.call,
            recovering=lambda: self.recovery,
            on_changed=self._changed,
            clock=self.clock,
            wall_clock=lambda: 1_760_000_000.0,
        )

    def _reset(self) -> str:
        self.resets.append("reset")
        return self.result

    def _changed(self) -> None:
        self.changes += 1


# ---- detection thresholds ---------------------------------------------------

def test_two_hfp_timeouts_in_a_row_trigger_one_reset(caplog) -> None:
    caplog.set_level(logging.INFO, logger="blueferry.profile_reset")
    h = Harness()
    h.subject.hfp_powered_failed(TIMEDOUT)
    assert h.resets == []
    h.subject.hfp_powered_failed(TIMEDOUT)
    assert h.resets == ["reset"]
    snapshot = h.subject.snapshot()
    assert snapshot["profile_reset_last"] == REASON_HFP
    assert snapshot["profile_reset_last_at"] == 1_760_000_000
    assert snapshot["profile_reset_last_auto"] is True
    assert snapshot["profile_reset_suggested"] == ""
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "HFP" in warnings[0].getMessage()


def test_other_hfp_errors_and_power_up_break_the_streak() -> None:
    h = Harness()
    h.subject.hfp_powered_failed(TIMEDOUT)
    h.subject.hfp_powered_failed("org.ofono.Error.Failed")
    h.subject.hfp_powered_failed(TIMEDOUT)
    h.subject.hfp_powered()
    h.subject.hfp_powered_failed(TIMEDOUT)
    assert h.resets == []


# ---- guards -------------------------------------------------------------------

@pytest.mark.parametrize("attribute", ["peer", "blocked", "call", "recovery"])
def test_no_reset_without_a_link_or_while_unsafe(attribute) -> None:
    h = Harness()
    setattr(h, attribute, attribute != "peer")
    h.subject.hfp_powered_failed(TIMEDOUT)
    h.subject.hfp_powered_failed(TIMEDOUT)
    assert h.resets == []
    assert h.subject.manual_reset() in {
        "disconnected", "bluez-unresponsive", "call-active", "recovery",
    }
    assert h.resets == []


def test_hfp_timeouts_without_a_link_do_not_count() -> None:
    h = Harness()
    h.peer = False
    h.subject.hfp_powered_failed(TIMEDOUT)
    h.peer = True
    h.subject.hfp_powered_failed(TIMEDOUT)
    assert h.resets == []


# ---- rate limit and opt-out ---------------------------------------------------

def _stick(h: Harness) -> None:
    h.subject.hfp_powered_failed(TIMEDOUT)
    h.subject.hfp_powered_failed(TIMEDOUT)


def test_at_most_one_automatic_reset_per_interval_with_backoff(caplog) -> None:
    caplog.set_level(logging.WARNING, logger="blueferry.profile_reset")
    h = Harness()
    _stick(h)
    assert h.resets == ["reset"]
    h.clock.now += AUTO_RESET_INTERVAL_SECONDS - 1
    _stick(h)
    _stick(h)
    assert h.resets == ["reset"]
    assert h.subject.snapshot()["profile_reset_suggested"] == REASON_HFP
    # Logged once per stuck episode, not per symptom.
    assert sum("recently" in r.getMessage() for r in caplog.records) == 1
    h.clock.now += 1
    _stick(h)
    assert h.resets == ["reset", "reset"]
    # Still unhealed: the next wait doubles.
    h.clock.now += AUTO_RESET_INTERVAL_SECONDS
    _stick(h)
    assert len(h.resets) == 2
    h.clock.now += AUTO_RESET_INTERVAL_SECONDS
    _stick(h)
    assert len(h.resets) == 3


def test_a_healthy_profile_ends_the_backoff() -> None:
    h = Harness()
    _stick(h)
    h.clock.now += AUTO_RESET_INTERVAL_SECONDS
    _stick(h)
    h.subject.hfp_powered()
    h.clock.now += AUTO_RESET_INTERVAL_SECONDS
    _stick(h)
    assert len(h.resets) == 3


def test_opt_out_only_suggests_the_manual_reset() -> None:
    h = Harness(enabled=False)
    _stick(h)
    assert h.resets == []
    assert h.subject.snapshot()["profile_reset_auto"] is False
    assert h.subject.suggested == REASON_HFP
    assert h.subject.manual_reset() == "started"
    snapshot = h.subject.snapshot()
    assert snapshot["profile_reset_last"] == REASON_MANUAL
    assert snapshot["profile_reset_last_auto"] is False
    assert snapshot["profile_reset_suggested"] == ""


def test_manual_reset_ignores_the_automatic_rate_limit() -> None:
    h = Harness()
    _stick(h)
    assert h.subject.manual_reset() == "started"
    assert len(h.resets) == 2


def test_peer_loss_drops_a_stale_suggestion() -> None:
    h = Harness(enabled=False)
    _stick(h)
    h.subject.peer_lost()
    assert h.subject.snapshot()["profile_reset_suggested"] == ""


def test_settings_json_overrides_the_environment(tmp_path, monkeypatch) -> None:
    path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "AUTO_PROFILE_RESET", True)
    assert profile_reset.auto_reset_enabled(path) is True
    path.write_text(json.dumps({"auto_profile_reset": False}))
    path.chmod(0o600)
    assert profile_reset.auto_reset_enabled(path) is False
    monkeypatch.setattr(config, "AUTO_PROFILE_RESET", False)
    path.write_text(json.dumps({"auto_profile_reset": "yes"}))
    assert profile_reset.auto_reset_enabled(path) is False
    assert "BLUEFERRY_AUTO_PROFILE_RESET" in config.LOCAL_ENV_KEYS


# ---- the reset itself (BearerSupervisor.reset_profiles) -----------------------

class Timers:
    def __init__(self) -> None:
        self.entries: dict[int, tuple[int, object]] = {}
        self._next = 1

    def schedule(self, delay, callback) -> int:
        source = self._next
        self._next += 1
        self.entries[source] = (delay, callback)
        return source

    def cancel(self, source) -> None:
        self.entries.pop(source, None)

    def fire(self, delay) -> None:
        for source, (wanted, callback) in list(self.entries.items()):
            if wanted == delay:
                self.entries.pop(source, None)
                callback()


def _bearers(state, *, blocked=lambda: False):
    timers = Timers()
    calls: list[tuple[str, str]] = []
    held: list[tuple] = []

    def connect(kind, ok, err):
        calls.append(("connect", kind))
        held.append((ok, err))

    def disconnect(kind, ok, err):
        calls.append(("disconnect", kind))
        held.append((ok, err))

    supervisor = BearerSupervisor(
        "/device", read_connected=state.get, connect=connect, disconnect=disconnect,
        bluez_blocked=blocked, schedule=timers.schedule, cancel=timers.cancel,
    )
    supervisor.start()
    return supervisor, timers, calls, held


def test_reset_disconnects_waits_then_connects_the_device() -> None:
    state = {"bredr": True, "le": False}
    supervisor, timers, calls, held = _bearers(state)
    calls.clear()
    assert supervisor.reset_profiles() == "started"
    assert calls == [("disconnect", "device")]
    assert supervisor.reconnect_snapshot()["state"] == "connecting"
    assert supervisor.reset_profiles() == "in-progress"
    assert supervisor.reconnect_now() == "in-progress"

    state["bredr"] = False
    held.pop()[0]()
    # The regular health tick must not dial underneath the reset.
    timers.fire(5)
    assert calls == [("disconnect", "device")]
    timers.fire(PROFILE_RESET_SETTLE_SECONDS)
    assert calls == [("disconnect", "device"), ("connect", "bredr")]
    assert supervisor.profile_reset_active is False


def test_reset_refuses_a_disconnected_or_unresponsive_peer() -> None:
    supervisor, _timers, calls, _held = _bearers({"bredr": False, "le": False})
    calls.clear()
    assert supervisor.reset_profiles() in {"disconnected", "in-progress"}
    assert ("disconnect", "device") not in calls

    blocked = [False]
    supervisor, _timers, calls, _held = _bearers(
        {"bredr": True, "le": False}, blocked=lambda: blocked[0],
    )
    blocked[0] = True
    assert supervisor.reset_profiles() == "bluez-unresponsive"
    assert ("disconnect", "device") not in calls


def test_reset_skips_the_connect_when_the_phone_returned_by_itself() -> None:
    state = {"bredr": True, "le": False}
    supervisor, timers, calls, held = _bearers(state)
    calls.clear()
    supervisor.reset_profiles()
    held.pop()[0]()
    timers.fire(PROFILE_RESET_SETTLE_SECONDS)
    assert calls == [("disconnect", "device")]


def test_failed_disconnect_ends_the_reset_and_not_connected_continues_it() -> None:
    state = {"bredr": True, "le": False}
    supervisor, timers, calls, held = _bearers(state)
    supervisor.reset_profiles()
    held.pop()[1](dbus.exceptions.DBusException("x", name="org.bluez.Error.Failed"))
    assert supervisor.profile_reset_active is False

    supervisor.reset_profiles()
    state["bredr"] = False
    held.pop()[1](dbus.exceptions.DBusException("x", name="org.bluez.Error.NotConnected"))
    calls.clear()
    timers.fire(PROFILE_RESET_SETTLE_SECONDS)
    assert calls == [("connect", "bredr")]


def test_bluez_restart_and_stop_abort_a_pending_reset() -> None:
    state = {"bredr": True, "le": False}
    supervisor, timers, _calls, held = _bearers(state)
    supervisor.reset_profiles()
    held.pop()[0]()
    supervisor.reset_after_bluez_restart()
    assert supervisor.profile_reset_active is False
    reconnect = supervisor._profile_reset_reconnect
    assert all(callback != reconnect for _delay, callback in timers.entries.values())
    supervisor.stop()
    assert supervisor.reset_profiles() == "unavailable"


# ---- client texts --------------------------------------------------------------

def test_reconnect_is_offered_for_a_stuck_profile_on_a_live_link() -> None:
    status = {"phone_reconnect_state": "connected", "profile_reset_suggested": REASON_HFP}
    view = reconnect_view(status)
    assert view.offered is True and view.attention is True and "HFP" in view.hint
    assert reconnect_view({"phone_reconnect_state": "connected"}).offered is False
    assert result_text("profile-reset") != result_text("unreachable")


# ---- daemon wiring ----------------------------------------------------------------

def test_reconnect_on_a_live_link_resets_a_stuck_profile(make_daemon) -> None:
    from types import SimpleNamespace

    instance = make_daemon()
    resets: list[str] = []
    instance.bearers = SimpleNamespace(reconnect_now=lambda: "connected")
    instance.profile_reset = SimpleNamespace(
        suggested=REASON_HFP, manual_reset=lambda: resets.append("x") or "started",
    )
    assert instance._reconnect_phone() == "profile-reset"
    instance.profile_reset = SimpleNamespace(suggested="", manual_reset=pytest.fail)
    assert instance._reconnect_phone() == "connected"
    assert resets == ["x"]


def test_daemon_wires_the_controller_and_reports_content_free_status(make_daemon) -> None:
    instance = make_daemon()
    assert instance.calls._on_powered_failed == instance.profile_reset.hfp_powered_failed
    assert instance.calls._on_powered == instance.profile_reset.hfp_powered
    snapshot = instance.profile_reset.snapshot()
    assert set(snapshot) == {
        "profile_reset_auto", "profile_reset_suggested", "profile_reset_last",
        "profile_reset_last_at", "profile_reset_last_auto",
    }
    assert all(isinstance(value, bool | int | str) for value in snapshot.values())
