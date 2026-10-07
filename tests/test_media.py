"""Opt-in media control policy, disabled paths, and daemon wiring."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from blueferry import daemon as daemon_mod
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
)


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
    with pytest.raises(NotReadyError, match="BLUEFERRY_MEDIA_CONTROL_ENABLED"):
        operations.send_media_command("play", lambda: None, lambda _error: None)


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

    def stop(self) -> None:
        self.stopped = True


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
