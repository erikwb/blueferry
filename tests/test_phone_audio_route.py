"""Hermetic tests for routing the iPhone's A2DP playback (fake bus, fake timers)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from gi.repository import GLib

from blueferry.backend_operations import BackendDependencies, BackendOperations
from blueferry.cli_audio import describe_playing, describe_route
from blueferry.errors import InvalidArgumentsError, NotReadyError, OperationFailedError
from blueferry.gio_dbus import DBusCallError
from blueferry.phone_audio_route import (
    A2DP_SINK_UUID,
    A2DP_SOURCE_UUID,
    PhoneAudioRoute,
)

DEV = "/org/bluez/hci0/dev_AA"
FD = f"{DEV}/fd0"


class Timers:
    def __init__(self) -> None:
        self.pending: dict[int, tuple[int, object]] = {}
        self._next = 0

    def schedule(self, seconds, callback) -> int:
        self._next += 1
        self.pending[self._next] = (seconds, callback)
        return self._next

    def cancel(self, source_id) -> None:
        self.pending.pop(source_id, None)

    def fire(self) -> None:
        for source_id, (_seconds, callback) in list(self.pending.items()):
            self.pending.pop(source_id, None)
            callback()


class Bus:
    def __init__(self, tree=None) -> None:
        self.tree = tree if tree is not None else {}
        self.calls: list[tuple] = []
        self.held: list[tuple] = []
        self.receivers: dict[str, object] = {}
        self.removed = 0
        self.types: list[tuple[str, str]] = []
        self.hold_probe = False
        self.held_probes: list[tuple] = []
        self.fail_subscribe_at: int | None = None

    def call(self, bus_name, path, interface, method, signature, args,
             reply_type, on_reply, on_error, *, timeout):
        # The real GioDBus builds exactly this variant; a type mismatch
        # between signature and args raises here as it would there.
        GLib.Variant(f"({signature})", args)
        assert GLib.VariantType.string_is_valid(reply_type)
        self.calls.append((path, interface, method, args))
        self.types.append((signature, reply_type))
        if method == "GetManagedObjects" and self.hold_probe:
            self.held_probes.append((on_reply, on_error))
        elif method == "GetManagedObjects":
            on_reply(self.tree)
        else:
            self.held.append((on_reply, on_error))

    def subscribe(self, sender, interface, member, handler, *, path=None, arg0=None,
                  with_path=False):
        assert sender == "org.bluez"
        if self.fail_subscribe_at is not None and len(self.receivers) == self.fail_subscribe_at:
            raise DBusCallError("org.freedesktop.DBus.Error.LimitsExceeded", "too many matches")
        if member == "PropertiesChanged" and arg0 == "org.bluez.MediaTransport1":
            assert with_path and path is None
            member = "TransportPropertiesChanged"
        self.receivers[member] = handler
        bus = self

        class Match:
            def remove(self) -> None:
                bus.removed += 1

        return Match()


def tree(*, connected=True, source=True, transport=False, state="idle"):
    uuids = [A2DP_SOURCE_UUID.upper()] if source else ["0000110e-0000-1000-8000-00805f9b34fb"]
    objects = {DEV: {"org.bluez.Device1": {"Connected": connected, "UUIDs": uuids}}}
    if transport:
        objects[FD] = {"org.bluez.MediaTransport1": {
            "Device": DEV, "UUID": A2DP_SINK_UUID, "State": state,
        }}
    return objects


def route(bus, *, allowed=True):
    timers = Timers()
    changes: list[int] = []
    subject = PhoneAudioRoute(
        bus, DEV, allowed=allowed, on_changed=lambda: changes.append(1),
        schedule=timers.schedule, cancel=timers.cancel,
    )
    return subject, timers, changes


def test_policy_forbids_the_sink_role_so_route_is_unavailable_and_inert() -> None:
    bus = Bus(tree(transport=True))
    subject, _timers, _changes = route(bus, allowed=False)
    subject.start()
    assert bus.calls == [] and bus.receivers == {}
    assert subject.snapshot() == {
        "phone_audio_route": "unavailable",
        "phone_audio_reason": "keep_phone_audio_on_phone",
        "phone_audio_pending": "",
        "phone_audio_playing": "unknown",
    }
    with pytest.raises(NotReadyError, match="KEEP_PHONE_AUDIO_ON_PHONE=false"):
        subject.set_route("pc", lambda _r: None, lambda _e: None)


@pytest.mark.parametrize(("objects", "expected"), [
    (tree(transport=True), ("pc", "")),
    (tree(), ("phone", "")),
    (tree(connected=False), ("unavailable", "phone_disconnected")),
    (tree(source=False), ("unavailable", "no_a2dp_source")),
    ({}, ("unavailable", "phone_disconnected")),
])
def test_probe_derives_route_from_device_and_transport(objects, expected) -> None:
    subject, _timers, _changes = route(Bus(objects))
    subject.start()
    snap = subject.snapshot()
    assert (snap["phone_audio_route"], snap["phone_audio_reason"]) == expected


def test_transport_of_another_device_or_role_is_ignored() -> None:
    objects = tree()
    objects["/org/bluez/hci0/dev_BB/fd1"] = {"org.bluez.MediaTransport1": {
        "Device": "/org/bluez/hci0/dev_BB", "UUID": A2DP_SINK_UUID}}
    objects[f"{DEV}/fd2"] = {"org.bluez.MediaTransport1": {
        "Device": DEV, "UUID": "0000110a-0000-1000-8000-00805f9b34fb"}}
    subject, _timers, _changes = route(Bus(objects))
    subject.start()
    assert subject.snapshot()["phone_audio_route"] == "phone"


def test_signals_follow_transport_and_disconnects() -> None:
    bus = Bus(tree())
    subject, _timers, changes = route(bus)
    subject.start()
    changes.clear()
    bus.receivers["InterfacesAdded"](FD, {"org.bluez.MediaTransport1": {
        "Device": DEV, "UUID": A2DP_SINK_UUID}})
    assert subject.snapshot()["phone_audio_route"] == "pc"
    bus.receivers["InterfacesRemoved"](FD, ["org.bluez.MediaTransport1"])
    assert subject.snapshot()["phone_audio_route"] == "phone"
    bus.receivers["PropertiesChanged"]("org.bluez.Device1", {"Connected": False}, [])
    assert subject.snapshot()["phone_audio_reason"] == "phone_disconnected"
    bus.receivers["PropertiesChanged"]("org.bluez.Device1", {"RSSI": -40}, [])
    assert len(changes) == 3
    subject.stop()
    assert bus.removed == 4


def test_live_signals_during_the_first_probe_do_not_discard_its_reply() -> None:
    # Review repro: the transport turns active while GetManagedObjects is
    # still out. The late reply must still set the device fields, otherwise
    # the route stays "unavailable/unknown" while the sound plays here.
    bus = Bus(tree(transport=True, state="active"))
    bus.hold_probe = True
    subject, _timers, _changes = route(bus)
    subject.start()
    bus.receivers["InterfacesAdded"](FD, {"org.bluez.MediaTransport1": {
        "Device": DEV, "UUID": A2DP_SINK_UUID, "State": "pending"}})
    transport_state(bus, "active")
    bus.receivers["PropertiesChanged"]("org.bluez.Device1", {"Connected": True}, [])
    assert subject.snapshot()["phone_audio_route"] == "unavailable"
    on_reply, _on_error = bus.held_probes.pop()
    on_reply(bus.tree)
    snap = subject.snapshot()
    assert (snap["phone_audio_route"], snap["phone_audio_reason"]) == ("pc", "")
    assert snap["phone_audio_playing"] == "pc"


def test_reply_of_a_superseded_probe_is_dropped() -> None:
    bus = Bus(tree())
    bus.hold_probe = True
    subject, _timers, _changes = route(bus)
    subject.start()
    subject.probe()
    newer_reply, _ = bus.held_probes.pop()
    older_reply, _ = bus.held_probes.pop()
    newer_reply(tree(transport=True))
    older_reply(tree(connected=False))
    assert subject.snapshot()["phone_audio_route"] == "pc"


@pytest.mark.parametrize("failing", [0, 2, 3])
def test_a_failed_watch_leaves_no_half_subscription(failing) -> None:
    bus = Bus(tree(transport=True))
    bus.fail_subscribe_at = failing
    subject, timers, _changes = route(bus)
    subject.start()  # must not raise into the daemon
    assert bus.removed == failing
    assert [call[2] for call in bus.calls] == []
    snap = subject.snapshot()
    assert (snap["phone_audio_route"], snap["phone_audio_reason"]) == ("unavailable", "unknown")
    assert [seconds for seconds, _cb in timers.pending.values()] == [30]
    subject.start()  # a second start while the retry waits adds no timer
    assert len(timers.pending) == 1
    bus.fail_subscribe_at = None
    bus.receivers.clear()
    timers.fire()  # the retry watches again and probes
    assert subject.snapshot()["phone_audio_route"] == "pc"
    assert timers.pending == {}


def test_stop_cancels_a_pending_watch_retry() -> None:
    bus = Bus(tree())
    bus.fail_subscribe_at = 1
    subject, timers, _changes = route(bus)
    subject.start()
    assert len(timers.pending) == 1
    subject.stop()
    assert timers.pending == {}


def test_switch_to_pc_calls_connect_profile_and_settles_with_a_reprobe() -> None:
    bus = Bus(tree())
    subject, timers, _changes = route(bus)
    subject.start()
    accepted: list[str] = []
    subject.set_route("pc", accepted.append, pytest.fail)
    assert bus.calls[-1] == (DEV, "org.bluez.Device1", "ConnectProfile", (A2DP_SOURCE_UUID,))
    assert subject.snapshot()["phone_audio_pending"] == "pc"
    with pytest.raises(NotReadyError, match="in progress"):
        subject.set_route("phone", accepted.append, pytest.fail)
    bus.tree = tree(transport=True)
    bus.held.pop()[0]()
    assert accepted == ["pc"]
    assert subject.snapshot()["phone_audio_pending"] == ""
    assert len(timers.pending) == 1
    timers.fire()
    assert bus.calls[-1][2] == "GetManagedObjects"
    assert subject.snapshot()["phone_audio_route"] == "pc"


def test_switch_to_phone_treats_not_connected_as_success() -> None:
    bus = Bus(tree(transport=True))
    subject, _timers, _changes = route(bus)
    subject.start()
    accepted: list[str] = []
    subject.set_route("phone", accepted.append, pytest.fail)
    assert bus.calls[-1][2] == "DisconnectProfile"
    bus.held.pop()[1](DBusCallError("org.bluez.Error.NotConnected", "x"))
    assert accepted == ["phone"]


def test_failure_is_reported_and_clears_pending() -> None:
    bus = Bus(tree())
    subject, _timers, _changes = route(bus)
    subject.start()
    errors: list[Exception] = []
    subject.set_route("pc", pytest.fail, errors.append)
    bus.held.pop()[1](DBusCallError("org.bluez.Error.Failed", "x"))
    assert len(errors) == 1
    assert subject.snapshot()["phone_audio_pending"] == ""


def test_invalid_route_and_unavailable_route_are_rejected() -> None:
    subject, _timers, _changes = route(Bus(tree(connected=False)))
    subject.start()
    with pytest.raises(InvalidArgumentsError):
        subject.set_route("speaker", pytest.fail, pytest.fail)
    with pytest.raises(NotReadyError):
        subject.set_route("pc", pytest.fail, pytest.fail)


def test_backend_operations_wraps_failures_as_audio_route_failed() -> None:
    class Audio:
        def snapshot(self):
            return {}

        def set_route(self, route, success, failure):
            failure(RuntimeError("boom"))

    seen: list[Exception] = []
    ops = BackendOperations(object(), BackendDependencies(phone_audio=Audio()))  # type: ignore[arg-type]
    ops.set_phone_audio_route("pc", pytest.fail, seen.append)
    assert isinstance(seen[0], OperationFailedError)
    assert seen[0].dbus_suffix == "AudioRouteFailed"
    with pytest.raises(NotReadyError):
        BackendOperations(object()).set_phone_audio_route(  # type: ignore[arg-type]
            "pc", pytest.fail, pytest.fail)


def test_cli_describes_every_state_without_content() -> None:
    assert "computer" in describe_route({"phone_audio_route": "pc"})
    assert "iPhone" in describe_route({"phone_audio_route": "phone"})
    assert "Switching" in describe_route({"phone_audio_pending": "pc"})
    assert "KEEP_PHONE_AUDIO_ON_PHONE=false" in describe_route({
        "phone_audio_route": "unavailable",
        "phone_audio_reason": "keep_phone_audio_on_phone",
    })
    assert "Update" in describe_route({})


def test_cli_describes_where_the_sound_plays() -> None:
    assert describe_playing({"phone_audio_route": "pc"}) == ""  # older backend
    assert "this computer" in describe_playing({"phone_audio_playing": "pc"})
    assert "on the iPhone" in describe_playing({"phone_audio_playing": "phone"})
    assert "Nothing" in describe_playing({"phone_audio_playing": "none"})
    assert "unknown" in describe_playing({"phone_audio_playing": "unknown"})
    assert "unknown" in describe_playing({"phone_audio_playing": "airplay"})
    assert "own speaker" in describe_playing({
        "phone_audio_route": "pc", "phone_audio_playing": "phone",
    })


def test_backend_status_keeps_the_playing_location() -> None:
    from blueferry.models import BackendStatus

    assert BackendStatus.from_dict({}).phone_audio_playing is None
    assert "phone_audio_playing" not in BackendStatus.from_dict({}).to_dict()
    status = BackendStatus.from_dict({"phone_audio_playing": "phone"})
    assert status.phone_audio_playing == "phone"
    assert status.to_dict()["phone_audio_playing"] == "phone"
    assert BackendStatus.from_dict({"phone_audio_playing": 3}).phone_audio_playing == "unknown"


def test_late_reply_after_stop_touches_no_timer_or_bus() -> None:
    bus = Bus(tree())
    subject, timers, changes = route(bus)
    subject.start()
    accepted: list[str] = []
    subject.set_route("pc", accepted.append, pytest.fail)
    reply = bus.held.pop()[0]
    subject.stop()
    assert subject.snapshot()["phone_audio_pending"] == ""
    calls, emitted = len(bus.calls), len(changes)
    reply()
    assert timers.pending == {}
    assert len(bus.calls) == calls and len(changes) == emitted


def test_in_progress_maps_to_not_ready_with_a_retry_hint() -> None:
    bus = Bus(tree())
    subject, _timers, _changes = route(bus)
    subject.start()
    errors: list[Exception] = []
    subject.set_route("pc", pytest.fail, errors.append)
    bus.held.pop()[1](DBusCallError("org.bluez.Error.InProgress", "x"))
    assert isinstance(errors[0], NotReadyError) and "try again" in str(errors[0]).casefold()

    class Audio:
        def snapshot(self):
            return {}

        def set_route(self, route, success, failure):
            failure(NotReadyError("busy"))

    seen: list[Exception] = []
    ops = BackendOperations(object(), BackendDependencies(phone_audio=Audio()))  # type: ignore[arg-type]
    ops.set_phone_audio_route("pc", pytest.fail, seen.append)
    assert isinstance(seen[0], NotReadyError)


def _resuming_route(bus, *, playing: bool):
    timers = Timers()
    resumed: list[int] = []
    subject = PhoneAudioRoute(
        bus, DEV, allowed=True, on_changed=lambda: None,
        schedule=timers.schedule, cancel=timers.cancel,
        was_playing=lambda: playing, resume_playback=lambda: resumed.append(1),
    )
    return subject, timers, resumed


def test_handback_to_phone_resumes_playback_that_ios_paused() -> None:
    bus = Bus(tree(transport=True))
    subject, timers, resumed = _resuming_route(bus, playing=True)
    subject.start()
    subject.set_route("phone", lambda _route: None, pytest.fail)
    bus.held.pop()[0]()
    assert resumed == []
    delays = sorted(seconds for seconds, _callback in timers.pending.values())
    assert delays == [2, 2]  # settle re-probe and the resume
    timers.fire()
    assert resumed == [1]


def test_handback_does_not_start_playback_that_was_paused() -> None:
    bus = Bus(tree(transport=True))
    subject, timers, resumed = _resuming_route(bus, playing=False)
    subject.start()
    subject.set_route("phone", lambda _route: None, pytest.fail)
    bus.held.pop()[0]()
    timers.fire()
    assert resumed == []


def test_switch_to_pc_never_resumes_and_stop_cancels_a_pending_resume() -> None:
    bus = Bus(tree())
    subject, timers, resumed = _resuming_route(bus, playing=True)
    subject.start()
    subject.set_route("pc", lambda _route: None, pytest.fail)
    bus.held.pop()[0]()
    timers.fire()
    assert resumed == []
    bus.tree = tree(transport=True)
    subject.probe()
    subject.set_route("phone", lambda _route: None, pytest.fail)
    bus.held.pop()[0]()
    subject.stop()
    timers.fire()
    assert resumed == []


def test_calls_state_their_wire_types() -> None:
    bus = Bus(tree())
    subject, _timers, _changes = route(bus)
    subject.start()
    subject.set_route("pc", lambda _route: None, pytest.fail)
    assert bus.types == [("", "(a{oa{sa{sv}}})"), ("s", "()")]


def test_failure_message_keeps_the_dbus_error_format() -> None:
    bus = Bus(tree())
    subject, _timers, _changes = route(bus)
    subject.start()
    seen: list[Exception] = []
    ops = BackendOperations(object(), BackendDependencies(phone_audio=subject))  # type: ignore[arg-type]
    ops.set_phone_audio_route("pc", pytest.fail, seen.append)
    bus.held.pop()[1](DBusCallError("org.bluez.Error.Failed", "br-connection-refused"))
    assert isinstance(seen[0], OperationFailedError)
    assert str(seen[0]) == "org.bluez.Error.Failed: br-connection-refused"


# ---- where the sound plays (phone_audio_playing) -----------------------------


class Media:
    """The AMS playback state the daemon passes in; notifies like its listener."""

    def __init__(self, state: str = "unknown") -> None:
        self.state = state
        self.subject: PhoneAudioRoute | None = None

    def __call__(self) -> str:
        return self.state

    def set(self, state: str) -> None:
        self.state = state
        assert self.subject is not None
        self.subject.media_changed()


def playing_route(bus, media: Media, *, allowed=True):
    timers = Timers()
    changes: list[int] = []
    subject = PhoneAudioRoute(
        bus, DEV, allowed=allowed, on_changed=lambda: changes.append(1),
        schedule=timers.schedule, cancel=timers.cancel, media_state=media,
    )
    media.subject = subject
    return subject, timers, changes


def playing(subject: PhoneAudioRoute) -> object:
    return subject.snapshot()["phone_audio_playing"]


def transport_state(bus: Bus, state: str, path: str = FD) -> None:
    bus.receivers["TransportPropertiesChanged"](
        path, "org.bluez.MediaTransport1", {"State": state}, [])


@pytest.mark.parametrize(("objects", "media", "expected"), [
    # Transport active: sound flows to this computer, whatever AMS says.
    (tree(transport=True, state="active"), "playing", "pc"),
    (tree(transport=True, state="active"), "paused", "pc"),
    (tree(transport=True, state="active"), "unknown", "pc"),
    # Stream open but idle (AirPlay set to the iPhone) or no stream at all.
    (tree(transport=True, state="idle"), "playing", "phone"),
    (tree(transport=True, state="pending"), "playing", "phone"),
    (tree(), "playing", "phone"),
    (tree(transport=True, state="idle"), "paused", "none"),
    (tree(), "paused", "none"),
    (tree(transport=True, state="idle"), "unknown", "unknown"),
    (tree(), "unknown", "unknown"),
    (tree(connected=False), "unknown", "unknown"),
])
def test_playing_location_combines_transport_state_and_ams(objects, media, expected) -> None:
    subject, timers, _changes = playing_route(Bus(objects), Media(media))
    subject.start()
    # The first value is published at once, not debounced.
    assert timers.pending == {}
    assert playing(subject) == expected


def test_route_pc_with_iphone_playing_on_itself_is_reported_as_phone() -> None:
    bus = Bus(tree(transport=True, state="active"))
    media = Media("playing")
    subject, timers, changes = playing_route(bus, media)
    subject.start()
    assert (subject.snapshot()["phone_audio_route"], playing(subject)) == ("pc", "pc")
    changes.clear()
    transport_state(bus, "idle")  # the user picked the iPhone in AirPlay
    assert playing(subject) == "pc" and changes == []
    assert [seconds for seconds, _cb in timers.pending.values()] == [2]
    timers.fire()
    assert (subject.snapshot()["phone_audio_route"], playing(subject)) == ("pc", "phone")
    assert changes == [1]


def test_track_change_gap_within_the_debounce_does_not_flicker() -> None:
    bus = Bus(tree(transport=True, state="active"))
    media = Media("playing")
    subject, timers, changes = playing_route(bus, media)
    subject.start()
    changes.clear()
    transport_state(bus, "idle")
    media.set("paused")
    media.set("playing")
    assert len(timers.pending) == 1  # one timer for the whole burst
    transport_state(bus, "active")
    assert timers.pending == {}
    assert playing(subject) == "pc" and changes == []


def test_change_to_pc_is_immediate_and_cancels_a_pending_change() -> None:
    bus = Bus(tree(transport=True, state="idle"))
    media = Media("paused")
    subject, timers, changes = playing_route(bus, media)
    subject.start()
    assert playing(subject) == "none"
    media.set("playing")
    assert playing(subject) == "none" and len(timers.pending) == 1
    changes.clear()
    transport_state(bus, "active")
    assert playing(subject) == "pc" and timers.pending == {}
    assert changes == [1]


def test_debounce_publishes_the_state_current_when_it_fires() -> None:
    bus = Bus(tree(transport=True, state="active"))
    media = Media("playing")
    subject, timers, _changes = playing_route(bus, media)
    subject.start()
    transport_state(bus, "idle")
    media.set("paused")
    timers.fire()
    assert playing(subject) == "none"
    media.set("unknown")
    timers.fire()
    assert playing(subject) == "unknown"


def test_a_change_that_reverts_before_the_debounce_cancels_it() -> None:
    bus = Bus(tree())
    media = Media("paused")
    subject, timers, changes = playing_route(bus, media)
    subject.start()
    changes.clear()
    media.set("playing")
    assert len(timers.pending) == 1
    media.set("paused")
    assert timers.pending == {} and changes == []


def test_transport_coming_and_going_follows_its_state() -> None:
    bus = Bus(tree())
    media = Media("playing")
    subject, timers, _changes = playing_route(bus, media)
    subject.start()
    assert playing(subject) == "phone"
    bus.receivers["InterfacesAdded"](FD, {"org.bluez.MediaTransport1": {
        "Device": DEV, "UUID": A2DP_SINK_UUID, "State": "pending"}})
    assert playing(subject) == "phone" and timers.pending == {}
    transport_state(bus, "active")
    assert playing(subject) == "pc"
    bus.receivers["InterfacesRemoved"](FD, ["org.bluez.MediaTransport1"])
    assert playing(subject) == "pc"
    timers.fire()
    assert playing(subject) == "phone"
    assert subject.snapshot()["phone_audio_route"] == "phone"


def test_state_of_unknown_transports_and_other_properties_is_ignored() -> None:
    bus = Bus(tree(transport=True, state="idle"))
    media = Media("paused")
    subject, timers, changes = playing_route(bus, media)
    subject.start()
    changes.clear()
    transport_state(bus, "active", path="/org/bluez/hci0/dev_BB/fd9")
    bus.receivers["TransportPropertiesChanged"](
        FD, "org.bluez.MediaTransport1", {"Volume": 90}, [])
    assert playing(subject) == "none" and changes == [] and timers.pending == {}


def test_disconnect_drops_the_active_transport() -> None:
    bus = Bus(tree(transport=True, state="active"))
    media = Media("playing")
    subject, timers, _changes = playing_route(bus, media)
    subject.start()
    bus.receivers["PropertiesChanged"]("org.bluez.Device1", {"Connected": False}, [])
    media.set("unknown")
    assert playing(subject) == "pc"
    timers.fire()
    assert playing(subject) == "unknown"


def test_ams_unknown_without_active_transport_is_unknown() -> None:
    bus = Bus(tree(transport=True, state="idle"))
    media = Media("playing")
    subject, timers, _changes = playing_route(bus, media)
    subject.start()
    media.set("unknown")
    timers.fire()
    assert playing(subject) == "unknown"


def test_policy_mode_still_reports_iphone_playback() -> None:
    bus = Bus(tree(transport=True, state="active"))
    media = Media("playing")
    subject, _timers, _changes = playing_route(bus, media, allowed=False)
    subject.start()
    assert bus.calls == [] and bus.receivers == {}
    assert subject.snapshot()["phone_audio_route"] == "unavailable"
    assert playing(subject) == "phone"


def test_failing_media_state_reads_as_unknown() -> None:
    def broken() -> str:
        raise RuntimeError("gone")

    timers = Timers()
    subject = PhoneAudioRoute(
        Bus(tree()), DEV, allowed=True, on_changed=lambda: None,
        schedule=timers.schedule, cancel=timers.cancel, media_state=broken,
    )
    subject.start()
    assert playing(subject) == "unknown"


def test_stop_cancels_the_debounce_and_ignores_media_changes() -> None:
    bus = Bus(tree(transport=True, state="active"))
    media = Media("playing")
    subject, timers, changes = playing_route(bus, media)
    subject.start()
    transport_state(bus, "idle")
    assert len(timers.pending) == 1
    subject.stop()
    assert timers.pending == {}
    changes.clear()
    media.set("paused")
    assert timers.pending == {} and changes == []


def test_without_media_state_the_location_is_unknown_unless_active() -> None:
    bus = Bus(tree(transport=True, state="active"))
    subject, _timers, _changes = route(bus)
    subject.start()
    assert playing(subject) == "pc"
    subject2, _t, _c = route(Bus(tree(transport=True)))
    subject2.start()
    assert playing(subject2) == "unknown"


def test_audio_status_command_reads_the_typed_playing_field(monkeypatch) -> None:
    """The typed key is not in BackendStatus.extra; the CLI must still see it."""
    from typer.testing import CliRunner

    from blueferry import cli_audio
    from blueferry.cli import app
    from blueferry.models import BackendStatus

    status = BackendStatus.from_dict({
        "daemon": True, "phone_audio_route": "pc", "phone_audio_reason": "",
        "phone_audio_pending": "", "phone_audio_playing": "phone",
    })
    assert "phone_audio_playing" not in status.extra

    class Client:
        def status(self) -> BackendStatus:
            return status

    monkeypatch.setattr(cli_audio, "BackendClient", Client)
    result = CliRunner().invoke(app, ["audio", "status"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert "Phone audio plays on this computer." in result.output
    assert "playing on its own speaker" in result.output


def test_daemon_announces_a_change_of_only_the_playing_location(make_daemon) -> None:
    """Route unchanged, playing location changed: clients get StatusChanged."""
    instance = make_daemon()
    emitted: list[int] = []
    instance._dbus_service = SimpleNamespace(emit_status=lambda: emitted.append(1))
    instance._idle_add = lambda callback, **_kwargs: callback()
    route = instance.phone_audio_route
    timers = Timers()
    route._schedule, route._cancel = timers.schedule, timers.cancel
    media = {"state": "paused"}
    route._media_state = lambda: media["state"]
    route.media_changed()  # the first value is published at once
    assert instance._status()["phone_audio_playing"] == "none"
    emitted.clear()
    before = {key: value for key, value in instance._status().items()
              if key.startswith("phone_audio_") and key != "phone_audio_playing"}
    media["state"] = "playing"  # AMS: playing, but no transport is active
    route.media_changed()
    assert emitted == []  # debounced
    timers.fire()
    assert emitted == [1]
    after = instance._status()
    assert after["phone_audio_playing"] == "phone"
    assert {key: after[key] for key in before} == before
    instance._dbus_service = None
