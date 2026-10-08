"""Opt-in media control policy, disabled paths, and daemon wiring."""
from __future__ import annotations

import functools
from types import SimpleNamespace

import pytest

from blueferry import daemon as daemon_mod
from blueferry.ams.client import AmsClient, AmsNotifySessions
from blueferry.ams.constants import EntityID, RemoteCommandID, TrackAttributeID
from blueferry.ams.parsers import EntityUpdate
from blueferry.backend_operations import BackendDependencies, BackendOperations
from blueferry.errors import InvalidArgumentsError, NotReadyError, OperationFailedError
from blueferry.media import (
    DETAIL_LE_UNKNOWN,
    DETAIL_READY,
    DETAIL_REQUIRES_LE,
    DETAIL_WAITING,
    MediaController,
    MediaControlSettings,
)
from tests.test_ams_client import DEVICE, EU, RC, _SessionBus
from tests.test_ams_client import _Timers as _GattTimers


class _Writer:
    def __init__(self, available: bool = True) -> None:
        self.available = available
        self.sent: list[tuple[RemoteCommandID, object, object]] = []

    def send_command(self, command, on_success, on_failure) -> None:
        self.sent.append((command, on_success, on_failure))


class _Timers:
    def __init__(self) -> None:
        self.pending: list = []

    def schedule(self, _delay, callback) -> int:
        self.pending.append(callback)
        return len(self.pending)

    def cancel(self, _source) -> None:
        self.pending.clear()

    def flush(self) -> None:
        pending, self.pending = self.pending, []
        for callback in pending:
            callback()


def _controller(*, available=True, supported=(), le_enabled=True):
    timers = _Timers()
    media = MediaController(
        clock=lambda: 100.0, schedule=timers.schedule, cancel=timers.cancel,
        le_enabled=le_enabled,
    )
    writer = _Writer(available)
    media.attach(writer)
    media.handle_supported_commands(frozenset(supported))
    timers.flush()
    return media, writer, timers


def _update(entity, attribute, value, truncated=False):
    return EntityUpdate(entity, attribute, truncated, value)


@pytest.mark.parametrize("name", ["", "stop", "PLAY ", "x" * 33, "next;rm"])
def test_unknown_or_malformed_command_names_are_invalid(name) -> None:
    media, writer, _ = _controller(supported=list(RemoteCommandID))
    if name.strip().casefold() == "play":
        media.resolve_command(name)
        return
    with pytest.raises(InvalidArgumentsError):
        media.resolve_command(name)
    assert writer.sent == []


def test_non_string_command_is_invalid() -> None:
    media, _, _ = _controller(supported=list(RemoteCommandID))
    with pytest.raises(InvalidArgumentsError):
        media.resolve_command(3)  # type: ignore[arg-type]


def test_commands_require_a_connected_media_link() -> None:
    media, writer, _ = _controller(available=False, supported=list(RemoteCommandID))
    with pytest.raises(NotReadyError):
        media.send_command("play", lambda: None, lambda _error: None)
    assert writer.sent == []


def test_only_advertised_commands_are_sent() -> None:
    media, writer, _ = _controller(supported=[RemoteCommandID.NextTrack])
    with pytest.raises(NotReadyError, match="does not currently offer"):
        media.send_command("like", lambda: None, lambda _error: None)
    media.send_command("next", lambda: None, lambda _error: None)
    assert [command for command, *_ in writer.sent] == [RemoteCommandID.NextTrack]


def test_toggle_and_play_pause_fall_back_to_the_advertised_variant() -> None:
    media, _, _ = _controller(supported=[RemoteCommandID.Play, RemoteCommandID.Pause])
    assert media.resolve_command("toggle") == RemoteCommandID.Play
    media.handle_update(_update(EntityID.Player, 1, "1,1.0,0"))
    assert media.resolve_command("toggle") == RemoteCommandID.Pause

    media, _, _ = _controller(supported=[RemoteCommandID.TogglePlayPause])
    assert media.resolve_command("play") == RemoteCommandID.TogglePlayPause
    with pytest.raises(NotReadyError):
        media.resolve_command("pause")  # already paused: toggling would play


def test_failed_gatt_write_becomes_a_stable_media_command_error() -> None:
    media, writer, _ = _controller(supported=[RemoteCommandID.Play])
    errors = []
    media.send_command("play", lambda: None, errors.append)
    _command, _success, failure = writer.sent[0]
    failure(RuntimeError("ATT 0xa0"))
    assert isinstance(errors[0], OperationFailedError)
    assert errors[0].dbus_suffix == "MediaCommandFailed"


def test_updates_are_coalesced_into_one_content_free_invalidation() -> None:
    media, _, timers = _controller()
    calls = []
    media.add_listener(lambda *args: calls.append(args))

    for attribute, value in ((0, "Artist"), (1, "Album"), (2, "Title"), (3, "200")):
        media.handle_update(_update(EntityID.Track, attribute, value))
    assert calls == []
    timers.flush()
    assert calls == [()]

    # An identical value is not a change.
    media.handle_update(_update(EntityID.Track, TrackAttributeID.Title, "Title"))
    assert timers.pending == []


def test_listener_failure_does_not_stop_other_listeners() -> None:
    media, _, timers = _controller()
    seen = []
    media.add_listener(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    media.add_listener(lambda: seen.append(True))
    media.handle_update(_update(EntityID.Track, 2, "Title"))
    timers.flush()
    assert seen == [True]


def test_snapshot_exposes_details_only_while_connected() -> None:
    media, writer, timers = _controller(supported=[RemoteCommandID.Play])
    media.handle_update(_update(EntityID.Track, 2, "Title"))
    snapshot = media.snapshot()
    assert snapshot["enabled"] is True
    assert snapshot["available"] is True
    assert snapshot["detail"] == DETAIL_READY
    assert snapshot["track"]["title"] == "Title"  # type: ignore[index]
    assert snapshot["supported_commands"] == ["play"]

    writer.available = False
    media.handle_availability(False)
    timers.flush()
    assert media.snapshot() == {"enabled": True, "available": False, "detail": DETAIL_WAITING}
    assert media.state.title is None
    # Review #207: iOS may not resend its command list after a reconnect, so
    # a soft reset keeps it; the AMS client clears it when it is really gone.
    assert media.state.supported_commands == frozenset({RemoteCommandID.Play})
    writer.available = True
    media.handle_availability(True)
    assert media.resolve_command("play") == RemoteCommandID.Play


def test_unknown_le_link_state_is_reported_instead_of_waiting() -> None:
    """Review #207: BlueZ without Bearer.LE1 never reports an LE link."""
    state: list[bool | None] = [None]
    media = MediaController(le_state=lambda: state[0])
    assert media.snapshot()["detail"] == DETAIL_LE_UNKNOWN
    state[0] = False
    assert media.snapshot()["detail"] == DETAIL_WAITING


def test_compatibility_mode_reports_why_media_is_unavailable() -> None:
    media = MediaController(le_enabled=False)
    assert media.snapshot() == {
        "enabled": True, "available": False, "detail": DETAIL_REQUIRES_LE,
    }


def test_backend_operations_are_inert_when_media_is_disabled() -> None:
    operations = BackendOperations(SimpleNamespace(map=None, pbap=None), BackendDependencies())
    assert operations.now_playing() == {
        "enabled": False, "available": False, "detail": "disabled",
    }
    with pytest.raises(NotReadyError, match="blueferry media enable"):
        operations.send_media_command("play", lambda: None, lambda _error: None)
    with pytest.raises(NotReadyError):
        operations.set_media_control(True)


def test_backend_operations_follow_the_current_media_controller() -> None:
    current: list = [None]
    operations = BackendOperations(
        SimpleNamespace(map=None, pbap=None),
        BackendDependencies(media=lambda: current[0]),
    )
    assert operations.now_playing()["enabled"] is False
    current[0] = MediaController()
    assert operations.now_playing()["enabled"] is True


def test_set_media_control_maps_errors() -> None:
    def failing(_enabled):
        raise OSError("disk full")

    operations = BackendOperations(
        SimpleNamespace(map=None, pbap=None),
        BackendDependencies(set_media_control=failing),
    )
    with pytest.raises(NotReadyError, match="could not save"):
        operations.set_media_control(True)


def test_media_settings_seed_from_local_env_and_saved_choice_wins(
    tmp_path, monkeypatch,
) -> None:
    path = tmp_path / "settings.json"
    monkeypatch.setattr(daemon_mod.config, "MEDIA_CONTROL_ENABLED", True)
    assert MediaControlSettings(path).enabled is True
    MediaControlSettings(path).set(False)
    assert MediaControlSettings(path).enabled is False
    MediaControlSettings(path).set(True)
    monkeypatch.setattr(daemon_mod.config, "MEDIA_CONTROL_ENABLED", False)
    assert MediaControlSettings(path).enabled is True
    with pytest.raises(ValueError):
        MediaControlSettings(path).set(1)  # type: ignore[arg-type]


def test_close_cancels_pending_invalidation() -> None:
    media, _, timers = _controller()
    calls = []
    media.add_listener(lambda: calls.append(True))
    media.handle_update(_update(EntityID.Track, 2, "Title"))
    media.close()
    assert timers.pending == []
    assert calls == []


# ---- daemon wiring ----------------------------------------------------------


def test_media_is_off_by_default_and_creates_no_ble_client(make_daemon) -> None:
    assert daemon_mod.config.MEDIA_CONTROL_ENABLED is False
    instance = make_daemon()
    assert instance.media is None

    instance._start_media("/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF")
    instance._observe_le_state(True)

    assert instance.ams is None


def test_default_status_reports_media_disabled(make_daemon, monkeypatch) -> None:
    instance = make_daemon()
    instance.contacts = SimpleNamespace(count=lambda: 0)
    instance.setup_verification = SimpleNamespace(verified=())
    monkeypatch.setattr(daemon_mod, "history_count", lambda **_kwargs: 0)
    status = instance._status()
    assert status["media_control_enabled"] is False
    assert status["media_control_available"] is False


def test_compatibility_mode_never_starts_ams(make_daemon, monkeypatch) -> None:
    monkeypatch.setattr(daemon_mod.config, "MEDIA_CONTROL_ENABLED", True)
    monkeypatch.setattr(daemon_mod.config, "ANCS_ENABLED", False)
    monkeypatch.setattr(
        daemon_mod, "AmsClient",
        lambda *_args, **_kwargs: pytest.fail("AMS must not start without LE"),
    )
    instance = make_daemon()
    instance._start_media("/device")
    assert instance.ams is None
    assert instance.media is not None
    assert instance.media.snapshot()["detail"] == DETAIL_REQUIRES_LE


class _FakeAms:
    def __init__(self, device_path, **callbacks) -> None:
        self.device_path = device_path
        self.callbacks = callbacks
        self.bearer = []
        self.owners = []
        self.started = False
        self.stopped = False
        self.available = False

    def observe_bearer_state(self, connected) -> None:
        self.bearer.append(connected)

    def observe_bluez_owner(self, old, new) -> None:
        self.owners.append((old, new))

    def start(self) -> None:
        self.started = True

    def stop(self, *, release=False, on_released=None) -> None:
        self.stopped = True
        self.release = release
        self.on_released = on_released


def test_enabled_media_follows_the_shared_le_bearer(make_daemon, monkeypatch) -> None:
    monkeypatch.setattr(daemon_mod.config, "MEDIA_CONTROL_ENABLED", True)
    monkeypatch.setattr(daemon_mod.config, "ANCS_ENABLED", True)
    monkeypatch.setattr(daemon_mod, "AmsClient", _FakeAms)
    instance = make_daemon()
    # Keep the controller's coalescing timer off the real GLib loop.
    assert instance.media is not None
    timers: list = []
    instance.media._schedule = lambda _ms, callback: timers.append(callback) or len(timers)
    instance.media._cancel = lambda _source: None
    statuses = []
    monkeypatch.setattr(instance, "_emit_status", lambda: statuses.append(True))
    instance.solicitation = SimpleNamespace(set_needed=lambda _needed: None, stop=lambda: None)

    instance._start_media("/device")
    ams = instance.ams
    assert isinstance(ams, _FakeAms)
    assert ams.started and ams.device_path == "/device"
    # It only observes the LE link the bearer supervisor already manages.
    assert ams.bearer == [instance.bearers.le_state]

    instance._observe_le_state(True)
    instance._observe_le_state(False)
    assert ams.bearer[-2:] == [True, False]

    ams.available = True
    ams.callbacks["on_availability"](True)
    assert statuses == [True]
    assert instance.media is not None and instance.media.available
    assert len(timers) == 1  # one coalesced invalidation is pending


def test_media_can_be_enabled_and_disabled_at_runtime(make_daemon, monkeypatch) -> None:
    """Review #207: a GUI/CLI opt-in instead of local.env plus restart."""
    monkeypatch.setattr(daemon_mod.config, "ANCS_ENABLED", True)
    monkeypatch.setattr(daemon_mod, "AmsClient", _FakeAms)
    instance = make_daemon()
    assert instance.media is None
    statuses = []
    monkeypatch.setattr(instance, "_emit_status", lambda: statuses.append(True))
    instance._start_media("/device")  # bluetooth init while opted out
    assert instance.ams is None

    result = instance._set_media_control(True)
    assert result == {"media_control_enabled": True, "media_control_available": False}
    ams = instance.ams
    assert isinstance(ams, _FakeAms) and ams.started and ams.device_path == "/device"
    assert instance.media is not None
    assert statuses == [True]
    assert MediaControlSettings().enabled is True

    result = instance._set_media_control(False)
    assert result == {"media_control_enabled": False, "media_control_available": False}
    assert ams.stopped
    assert instance.ams is None and instance.media is None
    assert MediaControlSettings().enabled is False
    # A restarted daemon keeps the saved choice.
    instance._set_media_control(True)
    assert make_daemon().media is not None


def _media_daemon(make_daemon, monkeypatch):
    """A real daemon and AMS client on the session-keeping fake BlueZ."""
    bus, timers = _SessionBus(), _GattTimers()
    fakes = {"bus_factory": lambda: bus, "schedule": timers.schedule, "cancel": timers.cancel}
    monkeypatch.setattr(daemon_mod.config, "ANCS_ENABLED", True)
    monkeypatch.setattr(daemon_mod, "AmsClient", functools.partial(AmsClient, **fakes))
    instance = make_daemon()
    instance.media_sessions = AmsNotifySessions(**fakes)
    monkeypatch.setattr(instance, "_emit_status", lambda: None)
    instance.solicitation = SimpleNamespace(set_needed=lambda _needed: None, stop=lambda: None)
    new_media = instance._new_media

    def inert_media():
        # Keep the controller's coalescing timer off the real GLib loop.
        media = new_media()
        media._schedule = lambda _ms, _callback: 0
        media._cancel = lambda _source: None
        return media

    monkeypatch.setattr(instance, "_new_media", inert_media)
    instance.bearers._states["le"] = True
    instance._start_media(DEVICE)
    return instance, bus, timers


def _run(bus, timers) -> None:
    bus.pump()
    timers.run_all()
    bus.pump()


def _set_le(instance, bus, connected: bool) -> None:
    if connected:
        bus.link_up()
    else:
        bus.link_down()
    instance.bearers._states["le"] = connected
    instance._observe_le_state(connected)


def _shown(instance):
    """(available, title, number of offered commands) as clients see them."""
    snapshot = instance.media.snapshot()
    return (
        snapshot["available"],
        (snapshot.get("track") or {}).get("title"),
        len(snapshot.get("supported_commands") or []),
    )


def _inert(instance, bus, timers) -> bool:
    return (
        instance.ams is None and bus.pending() == [] and timers.pending == {}
        and all(match.removed for match in bus.matches)
    )


def test_media_off_is_inert_on_every_link_change(make_daemon, monkeypatch) -> None:
    instance, bus, timers = _media_daemon(make_daemon, monkeypatch)

    _set_le(instance, bus, False)
    _set_le(instance, bus, True)
    _run(bus, timers)

    assert _inert(instance, bus, timers) and bus.log == [] and bus.matches == []


def test_opt_in_right_after_an_opt_out_waits_for_the_release(make_daemon, monkeypatch) -> None:
    instance, bus, timers = _media_daemon(make_daemon, monkeypatch)
    instance._set_media_control(True)
    _run(bus, timers)
    assert _shown(instance) == (True, "Song", 5)
    bus.hold = {"StopNotify"}

    instance._set_media_control(False)
    assert sorted(bus.pending()) == sorted([("StopNotify", RC), ("StopNotify", EU)])
    instance._set_media_control(True)
    instance._set_media_control(False)
    instance._set_media_control(True)
    # The old client's StopNotify replies are still outstanding.
    assert instance.ams is None

    bus.hold = set()
    bus.pump()
    assert instance.ams is not None
    _run(bus, timers)
    # The new StartNotify wrote the CCC, so iOS sent its command list again.
    assert _shown(instance) == (True, "Song", 5)
    assert bus.stops() == [RC, EU]


def test_a_release_without_a_new_opt_in_starts_nothing(make_daemon, monkeypatch) -> None:
    instance, bus, timers = _media_daemon(make_daemon, monkeypatch)
    instance._set_media_control(True)
    _run(bus, timers)

    instance._set_media_control(False)
    bus.pump()

    assert instance.media is None and bus.sessions == set()
    assert _inert(instance, bus, timers)
    _set_le(instance, bus, False)
    _set_le(instance, bus, True)
    assert _inert(instance, bus, timers)


@pytest.mark.parametrize("held", ["StartNotify", "WriteValue"])
def test_opt_out_during_a_subscription_does_not_cost_the_command_list(
    make_daemon, monkeypatch, held,
) -> None:
    """Review #207: the session survived and the next StartNotify wrote no CCC."""
    instance, bus, timers = _media_daemon(make_daemon, monkeypatch)
    instance._set_media_control(True)
    bus.pump()
    bus.hold = {held}
    timers.run_all()
    bus.pump()
    assert bus.pending() and not instance.media.available

    instance._set_media_control(False)
    instance._set_media_control(True)
    assert instance.ams is None
    bus.hold = set()
    bus.pump()

    # Released as soon as the start replies were in: the phone is quiet.
    assert bus.stops() and bus.sessions == set()
    _run(bus, timers)
    assert _shown(instance) == (True, "Song", 5)


@pytest.mark.parametrize("opt_in_while_down", [True, False])
def test_opt_out_while_le_is_down_does_not_cost_the_command_list(
    make_daemon, monkeypatch, opt_in_while_down,
) -> None:
    """Review #207: BlueZ re-enabled the surviving CCCs at link-up."""
    instance, bus, timers = _media_daemon(make_daemon, monkeypatch)
    instance._set_media_control(True)
    _run(bus, timers)
    _set_le(instance, bus, False)

    instance._set_media_control(False)
    assert _inert(instance, bus, timers) and bus.sessions == {RC, EU}
    if opt_in_while_down:
        instance._set_media_control(True)
    _set_le(instance, bus, True)
    if not opt_in_while_down:
        instance._set_media_control(True)

    # Neither call may run while BlueZ re-registers after the reconnect.
    assert instance.ams is None and bus.pending() == []
    assert [timers.delays[source] for source in timers.pending] == [3]
    timers.run_all()
    bus.pump()
    assert bus.stops() == [RC, EU] and instance.ams is not None
    _run(bus, timers)
    assert _shown(instance) == (True, "Song", 5)


def test_opt_out_right_after_a_reconnect_waits_for_the_link_to_settle(
    make_daemon, monkeypatch,
) -> None:
    """Review #207: StopNotify inside the settle window (bluetoothd 5.87 crash)."""
    instance, bus, timers = _media_daemon(make_daemon, monkeypatch)
    instance._set_media_control(True)
    _run(bus, timers)
    _set_le(instance, bus, False)
    _set_le(instance, bus, True)
    bus.log.clear()

    instance._set_media_control(False)
    bus.pump()

    assert bus.log == []
    assert [timers.delays[source] for source in timers.pending] == [3]
    timers.run_all()
    bus.pump()
    assert bus.log == [("StopNotify", RC), ("StopNotify", EU)]
    assert _inert(instance, bus, timers)


def test_a_failed_release_is_retried_before_the_next_opt_in_starts(
    make_daemon, monkeypatch,
) -> None:
    """Review #207: a failed StopNotify was treated as released."""
    instance, bus, timers = _media_daemon(make_daemon, monkeypatch)
    instance._set_media_control(True)
    _run(bus, timers)
    bus.failing_stops = 2

    instance._set_media_control(False)
    instance._set_media_control(True)
    bus.pump()

    assert instance.ams is None and bus.sessions == {RC, EU}
    assert [timers.delays[source] for source in timers.pending] == [2]
    timers.run_all()
    bus.pump()
    assert bus.sessions == set() and instance.ams is not None
    _run(bus, timers)
    assert _shown(instance) == (True, "Song", 5)


def test_shutdown_sends_no_stop_notify(make_daemon, monkeypatch) -> None:
    instance, bus, timers = _media_daemon(make_daemon, monkeypatch)
    instance._set_media_control(True)
    _run(bus, timers)

    instance.stop()
    bus.pump()

    # The closing bus connection ends the sessions.
    assert bus.stops() == [] and timers.pending == {}
    assert all(match.removed for match in bus.matches)


def test_a_release_answered_after_shutdown_starts_nothing(make_daemon, monkeypatch) -> None:
    instance, bus, timers = _media_daemon(make_daemon, monkeypatch)
    instance._set_media_control(True)
    _run(bus, timers)
    bus.hold = {"StopNotify"}
    instance._set_media_control(False)
    instance._set_media_control(True)  # waits for the release

    instance.stop()
    bus.hold = set()
    bus.pump()

    assert _inert(instance, bus, timers)


def test_runtime_opt_in_before_bluetooth_init_waits_for_it(make_daemon, monkeypatch) -> None:
    monkeypatch.setattr(daemon_mod.config, "ANCS_ENABLED", True)
    monkeypatch.setattr(daemon_mod, "AmsClient", _FakeAms)
    instance = make_daemon()
    monkeypatch.setattr(instance, "_emit_status", lambda: None)
    instance._set_media_control(True)
    assert instance.ams is None
    instance._start_media("/device")
    assert isinstance(instance.ams, _FakeAms)


def test_media_failure_cannot_abort_bluetoothd_restart_recovery(make_daemon, monkeypatch) -> None:
    """Review #207: an AMS exception skipped _on_bluez_restart()."""
    instance = make_daemon()

    class _Broken:
        def observe_bluez_owner(self, _old, _new):
            raise RuntimeError("NameHasNoOwner")

    instance.ams = _Broken()  # type: ignore[assignment]
    instance._bluez_owner_match = object()
    instance.ancs = None
    instance.recovery = SimpleNamespace(active=False, invalidate=lambda: None)
    instance.solicitation = SimpleNamespace(reset_after_bluez_restart=lambda: None)
    monkeypatch.setattr(daemon_mod.bluez_setup, "forget_advert_registration", lambda: None)
    restarted = []
    monkeypatch.setattr(instance, "_on_bluez_restart", lambda: restarted.append(True))

    instance._on_bluez_owner_changed("org.bluez", ":1.1", ":1.2")

    assert restarted == [True]


def test_media_failure_cannot_break_le_state_propagation(make_daemon) -> None:
    instance = make_daemon()
    seen = []

    class _Broken:
        def observe_bearer_state(self, _connected):
            raise RuntimeError("boom")

    instance.ams = _Broken()  # type: ignore[assignment]
    instance.ancs = SimpleNamespace(observe_bearer_state=seen.append)
    instance.solicitation = SimpleNamespace(set_needed=lambda _needed: None)
    instance._observe_le_state(True)  # must not raise into the supervisor
    assert seen == [True]
