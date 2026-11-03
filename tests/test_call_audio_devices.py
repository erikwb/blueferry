"""Call speaker/microphone routing over recorded pw-dump data; nothing runs PipeWire."""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from blueferry.calls import audio_devices
from blueferry.calls.audio_devices import (
    FAST_INTERVAL_SEC,
    INPUT,
    ORIGIN_CALL,
    ORIGIN_SETTINGS,
    ORIGIN_SYSTEM,
    OUTPUT,
    STATE_IDLE,
    STATE_READY,
    STATE_UNAVAILABLE,
    STATE_WAITING,
    STEADY_INTERVAL_SEC,
    CallAudioRouter,
    Choice,
    parse_dump,
    plan,
    valid_node_name,
)
from blueferry.errors import CommandError, InvalidArgumentsError, NotReadyError

MAC = "78:A7:C7:A7:CD:02"
OTHER_MAC = "11:22:33:44:55:66"
SPEAKERS = "alsa_output.pci-0000_00_1f.3.analog-stereo"
HEADSET = "alsa_output.usb-Logitech_A50-00.stereo-chat"
HEADSET_MIC = "alsa_input.usb-Logitech_A50-00.mono-chat"
WEBCAM_MIC = "alsa_input.usb-046d_HD_Pro_Webcam_C920.analog-stereo"


def _node(node_id, serial, name, media_class, description="", **props):
    return {
        "id": node_id, "type": audio_devices.NODE_TYPE,
        "info": {"props": {
            "object.serial": serial, "node.name": name, "media.class": media_class,
            **({"node.description": description} if description else {}), **props,
        }},
    }


def _link(output, input_):
    return {"id": 900 + output + input_, "type": audio_devices.LINK_TYPE,
            "info": {"output-node-id": output, "input-node-id": input_}}


def _metadata(targets=None, name="default"):
    entries = [
        {"subject": 0, "key": "default.audio.sink", "type": "Spa:String:JSON",
         "value": {"name": SPEAKERS}},
        {"subject": 0, "key": "default.audio.source", "type": "Spa:String:JSON",
         "value": json.dumps({"name": HEADSET_MIC})},
    ]
    for subject, value in (targets or {}).items():
        entries.append({"subject": subject, "key": "target.object", "type": "Spa:Id",
                        "value": value})
    return {"id": 42, "type": audio_devices.METADATA_TYPE,
            "props": {"metadata.name": name}, "metadata": entries}


def _dump(*, call=True, targets=None, extra=(), links=None):
    """The graph seen during a real iPhone call (2026-10-27), IDs shortened."""
    mac_token = MAC.replace(":", "_")
    objects = [
        _node(45, 65, SPEAKERS, "Audio/Sink", "Built-in Audio Analog Stereo"),
        _node(48, 62, HEADSET, "Audio/Sink", "A50 Chat"),
        _node(47, 63, HEADSET_MIC, "Audio/Source", "A50 Chat"),
        _node(46, 64, WEBCAM_MIC, "Audio/Source", "HD Pro Webcam C920"),
        # The phone's own A2DP media source is no microphone for calls.
        _node(80, 80, f"bluez_input.{mac_token}.a2dp-source", "Audio/Source", "iPhone",
              **{"api.bluez5.address": MAC}),
        _node(81, 81, "alsa_input.virtual", "Audio/Source/Virtual", "Virtual"),
        _metadata(targets),
        *extra,
    ]
    if call:
        objects += [
            _node(103, 1103, f"bluez_input.{mac_token}.0", "Stream/Output/Audio", "iPhone",
                  **{"api.bluez5.address": MAC,
                     "api.bluez5.profile": "headset-audio-gateway"}),
            _node(104, 1104, f"bluez_output.{mac_token}.1", "Stream/Input/Audio", "iPhone",
                  **{"api.bluez5.address": MAC}),
        ]
        objects += links if links is not None else [_link(103, 45), _link(47, 104)]
    return json.dumps(objects)


class Settings:
    def __init__(self, output="", input_=""):
        self.values = {OUTPUT: output, INPUT: input_}
        self.saved: list[tuple[str, str]] = []

    def get(self, direction):
        return self.values[direction]

    def set(self, direction, name):
        self.values[direction] = name
        self.saved.append((direction, name))
        return name


class FakePipeWire:
    """Answers pw-dump with the current dump and records pw-metadata."""

    def __init__(self, dump=None):
        self.dump = dump if dump is not None else _dump()
        self.commands: list[list[str]] = []
        self.missing = False
        self.verified: list[int] = []
        self.verify_dump: str | None = None
        self.metadata_error = False
        self.dump_error = False

    def __call__(self, argv, *, timeout):
        assert timeout <= 5
        if self.missing:
            raise CommandError(tuple(argv), f"{argv[0]} is not installed")
        if argv == ["pw-dump"]:
            if self.dump_error:
                raise CommandError(tuple(argv), "pw-dump timed out after 5 seconds")
            return SimpleNamespace(stdout=self.dump)
        if argv[0] == "pw-dump":  # one object, read again before a write
            self.verified.append(int(argv[1]))
            dump = self.verify_dump if self.verify_dump is not None else self.dump
            objects = [item for item in json.loads(dump) if item.get("id") == int(argv[1])]
            return SimpleNamespace(stdout=json.dumps(objects))
        if self.metadata_error:
            raise CommandError(tuple(argv), "pw-metadata failed")
        self.commands.append(list(argv))
        return SimpleNamespace(stdout="")


class Loop:
    """Synchronous worker and inspectable timers."""

    def __init__(self):
        self.timers: dict[int, tuple[int, object]] = {}
        self.next_id = 1
        self.deferred: list = []
        self.defer = False
        self.full = False

    def submit(self, operation, *, on_success, on_error):
        if self.full:
            raise RuntimeError("background worker is unavailable or busy")

        def run():
            try:
                value = operation()
            except Exception as error:
                on_error(error)
            else:
                on_success(value)

        if self.defer:
            self.deferred.append(run)
        else:
            run()

    def schedule(self, delay, callback):
        source = self.next_id
        self.next_id += 1
        self.timers[source] = (delay, callback)
        return source

    def cancel(self, source):
        self.timers.pop(source, None)

    def fire(self):
        source, (_delay, callback) = next(iter(self.timers.items()))
        del self.timers[source]
        callback()

    def delays(self):
        return [delay for delay, _callback in self.timers.values()]


def _router(settings=None, pipewire=None, loop=None):
    loop = loop or Loop()
    pipewire = pipewire or FakePipeWire()
    changes: list[int] = []
    router = CallAudioRouter(
        settings=settings or Settings(), mac=MAC, submit=loop.submit, run=pipewire,
        schedule=loop.schedule, cancel=loop.cancel, on_changed=lambda: changes.append(1),
    )
    return router, pipewire, loop, changes


# ---- parsing and planning -------------------------------------------------


def test_device_lists_leave_out_the_phone_virtual_nodes_and_streams() -> None:
    listed = audio_devices.device_list(parse_dump(_dump()), MAC)

    assert listed["outputs"] == [
        {"name": HEADSET, "description": "A50 Chat", "default": False},
        {"name": SPEAKERS, "description": "Built-in Audio Analog Stereo", "default": True},
    ]
    assert [item["name"] for item in listed["inputs"]] == [HEADSET_MIC, WEBCAM_MIC]
    assert listed["inputs"][0]["default"] is True


def test_parsing_survives_garbage_and_strips_control_characters() -> None:
    assert parse_dump("not json") == audio_devices.Graph()
    assert parse_dump('{"a": 1}') == audio_devices.Graph()
    graph = parse_dump(json.dumps([
        None, {"type": audio_devices.NODE_TYPE, "id": "x"},
        _node(5, 6, "alsa_output.evil", "Audio/Sink", "Evil\x1b[2J"),
        {"type": audio_devices.LINK_TYPE, "info": {"output-node-id": None}},
        _metadata(name="settings", targets={5: "1"}),
    ]))
    assert [node.description for node in graph.nodes.values()] == ["Evil[2J"]
    assert graph.links == () and graph.targets == {}


def test_streams_are_bound_to_the_selected_phone() -> None:
    other = OTHER_MAC.replace(":", "_")
    graph = parse_dump(_dump(call=False, extra=[
        _node(200, 1200, f"bluez_input.{other}.0", "Stream/Output/Audio",
              **{"api.bluez5.address": OTHER_MAC}),
    ]))

    assert audio_devices.call_stream(graph, MAC, OUTPUT) is None
    assert audio_devices.call_stream(graph, OTHER_MAC, OUTPUT).id == 200


def test_system_choice_reports_the_linked_devices_without_commands() -> None:
    commands, report = plan(parse_dump(_dump()), MAC, {})

    assert commands == []
    assert report[OUTPUT] == {
        "selected": "", "origin": ORIGIN_SYSTEM, "missing": False, "stream": True,
        "device": SPEAKERS, "description": "Built-in Audio Analog Stereo",
    }
    assert report[INPUT]["device"] == HEADSET_MIC


def test_a_chosen_device_moves_only_the_call_stream() -> None:
    writes, _report = plan(parse_dump(_dump()), MAC, {
        OUTPUT: Choice(HEADSET, ORIGIN_SETTINGS), INPUT: Choice(WEBCAM_MIC, ORIGIN_CALL),
    })

    assert [write.argv for write in writes] == [
        ["pw-metadata", "-n", "default", "103", "target.object", "62", "Spa:Id"],
        ["pw-metadata", "-n", "default", "104", "target.object", "64", "Spa:Id"],
    ]


def test_a_target_already_in_place_is_not_written_again() -> None:
    graph = parse_dump(_dump(targets={103: "62"}, links=[_link(103, 48), _link(47, 104)]))

    commands, report = plan(graph, MAC, {OUTPUT: Choice(HEADSET, ORIGIN_SETTINGS)})

    assert commands == []
    assert report[OUTPUT]["description"] == "A50 Chat"


def test_same_as_system_deletes_a_target_wireplumber_restored() -> None:
    graph = parse_dump(_dump(targets={103: 62, 104: "64"}))

    writes, _report = plan(graph, MAC, {})

    assert [write.argv for write in writes] == [
        ["pw-metadata", "-n", "default", "-d", "103", "target.object"],
        ["pw-metadata", "-n", "default", "-d", "104", "target.object"],
    ]


def test_a_missing_device_falls_back_to_the_system_and_says_so() -> None:
    graph = parse_dump(_dump(targets={103: "999"}))

    writes, report = plan(graph, MAC, {OUTPUT: Choice("alsa_output.gone", ORIGIN_SETTINGS)})

    assert [write.argv for write in writes] == [["pw-metadata", "-n", "default", "-d", "103", "target.object"]]
    assert report[OUTPUT]["missing"] is True
    assert report[OUTPUT]["device"] == SPEAKERS


def test_the_phone_itself_cannot_be_chosen() -> None:
    phone_source = f"bluez_input.{MAC.replace(':', '_')}.a2dp-source"

    commands, report = plan(parse_dump(_dump()), MAC, {INPUT: Choice(phone_source, ORIGIN_CALL)})

    assert commands == [] and report[INPUT]["missing"] is True


def test_without_call_streams_nothing_is_written() -> None:
    commands, report = plan(parse_dump(_dump(call=False)), MAC, {
        OUTPUT: Choice(HEADSET, ORIGIN_SETTINGS),
    })

    assert commands == []
    assert report[OUTPUT]["stream"] is False and report[OUTPUT]["device"] == ""


@pytest.mark.parametrize("value", [None, 5, "a\nb", "x" * 256, "\x7f"])
def test_invalid_device_names_are_refused(value) -> None:
    with pytest.raises(InvalidArgumentsError):
        valid_node_name(value)


def test_device_names_are_trimmed() -> None:
    assert valid_node_name(f"  {HEADSET} ") == HEADSET
    assert valid_node_name("") == ""


# ---- the router -------------------------------------------------------------


def test_router_is_inert_without_a_call() -> None:
    router, pipewire, loop, changes = _router(Settings(output=HEADSET))

    assert router.call_audio()["state"] == STATE_IDLE
    assert router.snapshot() == {"calls_audio_output": HEADSET, "calls_audio_input": ""}
    assert pipewire.commands == [] and loop.timers == {} and changes == []
    with pytest.raises(NotReadyError):
        router.set_for_call(OUTPUT, HEADSET)


def test_router_waits_for_the_streams_then_applies_the_saved_choice() -> None:
    pipewire = FakePipeWire(_dump(call=False))
    router, pipewire, loop, changes = _router(Settings(output=HEADSET), pipewire)

    router.calls_changed(True)
    assert router.call_audio()["state"] == STATE_WAITING
    assert loop.delays() == [FAST_INTERVAL_SEC]
    assert pipewire.commands == []

    pipewire.dump = _dump()  # SCO is up: the call streams appear
    loop.fire()

    assert pipewire.commands == [
        ["pw-metadata", "-n", "default", "103", "target.object", "62", "Spa:Id"],
    ]
    audio = router.call_audio()
    assert audio["state"] == STATE_READY
    assert audio[OUTPUT]["origin"] == ORIGIN_SETTINGS and audio[OUTPUT]["selected"] == HEADSET
    assert changes  # CallsChanged for the call view
    assert loop.delays() == [FAST_INTERVAL_SEC]  # look again soon after a move

    pipewire.dump = _dump(targets={103: "62"}, links=[_link(103, 48), _link(47, 104)])
    loop.fire()
    assert router.call_audio()[OUTPUT]["description"] == "A50 Chat"
    assert len(pipewire.commands) == 1
    assert loop.delays() == [STEADY_INTERVAL_SEC]  # settled: watch for hot-plugging


def test_hot_plugged_device_is_used_when_it_appears() -> None:
    pipewire = FakePipeWire(_dump())
    router, pipewire, loop, _changes = _router(Settings(output="alsa_output.usb-new"), pipewire)

    router.calls_changed(True)
    assert router.call_audio()[OUTPUT]["missing"] is True
    assert pipewire.commands == []

    pipewire.dump = _dump(extra=[_node(60, 70, "alsa_output.usb-new", "Audio/Sink", "New")])
    while loop.timers:
        loop.fire()
        if pipewire.commands:
            break

    assert pipewire.commands == [
        ["pw-metadata", "-n", "default", "103", "target.object", "70", "Spa:Id"],
    ]


def test_choice_for_this_call_wins_and_ends_with_the_call() -> None:
    settings = Settings(output=HEADSET)
    router, pipewire, loop, _changes = _router(settings)
    router.calls_changed(True)
    pipewire.commands.clear()

    router.set_for_call(OUTPUT, "")

    # The router read the old graph (no target set yet): nothing to delete.
    assert pipewire.commands == []
    assert router.call_audio()[OUTPUT]["origin"] == ORIGIN_CALL
    assert settings.saved == []

    router.set_for_call(INPUT, WEBCAM_MIC)
    assert pipewire.commands[-1][3:6] == ["104", "target.object", "64"]

    router.calls_changed(False)
    assert loop.timers == {}
    audio = router.call_audio()
    assert audio["state"] == STATE_IDLE
    assert audio[OUTPUT]["origin"] == ORIGIN_SETTINGS
    assert audio[INPUT]["origin"] == ORIGIN_SYSTEM


def test_saving_a_choice_during_a_call_applies_it_at_once() -> None:
    settings = Settings()
    router, pipewire, _loop, _changes = _router(settings)
    router.calls_changed(True)

    assert router.set_preferred(OUTPUT, HEADSET) == {
        "calls_audio_output": HEADSET, "calls_audio_input": "",
    }
    assert settings.saved == [(OUTPUT, HEADSET)]
    assert pipewire.commands[-1][3:6] == ["103", "target.object", "62"]


def test_saving_without_a_call_runs_nothing() -> None:
    router, pipewire, loop, _changes = _router()

    router.set_preferred(INPUT, WEBCAM_MIC)

    assert pipewire.commands == [] and loop.timers == {}
    with pytest.raises(InvalidArgumentsError):
        router.set_preferred("speaker", HEADSET)


def test_results_of_an_ended_call_are_dropped() -> None:
    loop = Loop()
    loop.defer = True
    router, _pipewire, loop, _changes = _router(Settings(output=HEADSET), loop=loop)

    router.calls_changed(True)
    router.calls_changed(False)
    for run in loop.deferred:
        run()

    assert router.call_audio()["state"] == STATE_IDLE
    assert loop.timers == {}
    # The worker still ran its job, but nothing follows from it.
    assert router.call_audio()[OUTPUT]["stream"] is False


def test_a_check_requested_while_one_runs_runs_once_more() -> None:
    loop = Loop()
    loop.defer = True
    router, pipewire, loop, _changes = _router(loop=loop)
    router.calls_changed(True)
    router.set_for_call(OUTPUT, HEADSET)
    router.set_for_call(INPUT, WEBCAM_MIC)
    assert len(loop.deferred) == 1

    loop.deferred.pop(0)()
    assert len(loop.deferred) == 1  # the queued re-check
    loop.deferred.pop(0)()
    assert {tuple(argv[3:6]) for argv in pipewire.commands} == {
        ("103", "target.object", "62"), ("104", "target.object", "64"),
    }


def test_missing_pipewire_tools_report_unavailable_and_log_once(caplog) -> None:
    pipewire = FakePipeWire()
    pipewire.missing = True
    router, pipewire, loop, changes = _router(pipewire=pipewire)

    with caplog.at_level(logging.WARNING, logger="blueferry.calls.audio_devices"):
        router.calls_changed(True)
        loop.fire()

    assert router.call_audio()["state"] == STATE_UNAVAILABLE
    assert changes == [1]
    assert len([r for r in caplog.records if "could not route" in r.message]) == 1
    assert loop.delays() == [STEADY_INTERVAL_SEC]

    pipewire.missing = False
    loop.fire()
    assert router.call_audio()["state"] == STATE_READY


def test_listing_devices_adds_the_saved_choices() -> None:
    router, _pipewire, _loop, _changes = _router(Settings(output="alsa_output.gone"))
    replies, errors = [], []

    router.list_devices(replies.append, errors.append)

    assert errors == []
    assert replies[0]["output"] == "alsa_output.gone" and replies[0]["input"] == ""
    assert [item["name"] for item in replies[0]["outputs"]] == [HEADSET, SPEAKERS]


def test_listing_devices_failures_are_not_ready() -> None:
    pipewire = FakePipeWire()
    pipewire.missing = True
    router, _pipewire, loop, _changes = _router(pipewire=pipewire)
    errors: list[Exception] = []

    router.list_devices(lambda _payload: None, errors.append)
    assert isinstance(errors[0], NotReadyError)

    loop.full = True
    with pytest.raises(NotReadyError):
        router.list_devices(lambda _payload: None, errors.append)


def test_a_call_answered_late_is_checked_every_second_again() -> None:
    pipewire = FakePipeWire(_dump(call=False))
    router, pipewire, loop, _changes = _router(Settings(output=HEADSET), pipewire)
    router.calls_changed(True)
    while loop.delays() == [FAST_INTERVAL_SEC]:
        loop.fire()  # rings for a long time: no call audio yet
    assert loop.delays() == [STEADY_INTERVAL_SEC]

    router.calls_changed(True)  # answered: CallsChanged from the controller

    assert loop.delays() == [FAST_INTERVAL_SEC]
    pipewire.dump = _dump()
    loop.fire()
    assert pipewire.commands[-1][3:6] == ["103", "target.object", "62"]


def test_a_recycled_node_id_is_never_written() -> None:
    pipewire = FakePipeWire()
    # Between the dump and the write, ID 103 went to another node.
    pipewire.verify_dump = json.dumps([_node(103, 5555, "alsa_output.other", "Audio/Sink")])
    router, pipewire, _loop, _changes = _router(Settings(output=HEADSET), pipewire)

    router.calls_changed(True)

    assert pipewire.verified == [103]
    assert pipewire.commands == []


def test_a_failed_write_keeps_the_report_and_looks_again_soon() -> None:
    pipewire = FakePipeWire()
    pipewire.metadata_error = True
    router, pipewire, loop, _changes = _router(Settings(output=HEADSET), pipewire)

    router.calls_changed(True)

    assert router.call_audio()["state"] == STATE_READY
    assert router.call_audio()[OUTPUT]["description"] == "Built-in Audio Analog Stereo"
    assert loop.delays() == [FAST_INTERVAL_SEC]
    pipewire.metadata_error = False
    loop.fire()
    assert pipewire.commands[-1][3:6] == ["103", "target.object", "62"]


def test_one_failed_read_keeps_the_view_two_make_it_unavailable() -> None:
    router, pipewire, loop, changes = _router()
    router.calls_changed(True)
    assert router.call_audio()["state"] == STATE_READY
    changes.clear()

    pipewire.dump_error = True
    loop.fire()
    assert router.call_audio()["state"] == STATE_READY
    assert router.call_audio()[OUTPUT]["device"] == SPEAKERS
    assert loop.delays() == [FAST_INTERVAL_SEC] and changes == []

    loop.fire()
    assert router.call_audio()["state"] == STATE_UNAVAILABLE

    pipewire.dump_error = False
    loop.fire()
    assert router.call_audio()["state"] == STATE_READY


def test_switching_calls_off_releases_the_remembered_targets() -> None:
    pipewire = FakePipeWire(_dump(targets={103: "62"}))
    router, pipewire, _loop, _changes = _router(pipewire=pipewire)

    router.release()

    assert pipewire.commands == [["pw-metadata", "-n", "default", "-d", "103", "target.object"]]
    pipewire.commands.clear()
    pipewire.dump = _dump(call=False)
    router.release()
    assert pipewire.commands == []


# ---- muting the call microphone ---------------------------------------------

MIC_STREAM = 104


def _mic(muted=None, *, serial=1104, node_id=MIC_STREAM, **kwargs):
    """A call dump whose microphone stream reports ``mute`` in its Props."""
    objects = json.loads(_dump(**kwargs))
    for item in objects:
        if item.get("id") == MIC_STREAM and item.get("type") == audio_devices.NODE_TYPE:
            item["id"] = node_id
            item["info"]["props"]["object.serial"] = serial
            if muted is not None:
                # Real dumps list several Props entries; only one has "mute".
                item["info"]["params"] = {"Props": [{"volume": 1.0}, {"mute": muted}]}
    return json.dumps(objects)


def _mute_commands(pipewire):
    return [argv for argv in pipewire.commands if argv[0] == "pw-cli"]


def test_parsing_reads_the_stream_mute_from_its_props() -> None:
    graph = parse_dump(_mic(True))

    assert graph.nodes[MIC_STREAM].muted is True
    assert graph.nodes[103].muted is None
    broken = json.loads(_dump())
    broken[-3]["info"]["params"] = {"Props": "nonsense"}
    assert all(node.muted is None for node in parse_dump(json.dumps(broken)).nodes.values())


def test_mute_write_targets_only_the_call_microphone_stream() -> None:
    write, serial = audio_devices.mute_write(parse_dump(_mic(False)), MAC, True, None)

    assert serial == 1104
    assert write.argv == ["pw-cli", "set-param", "104", "Props", "{ mute: true }"]
    # Nothing to do: already in line, already settled, or no call stream.
    assert audio_devices.mute_write(parse_dump(_mic(True)), MAC, True, None) == (None, 1104)
    assert audio_devices.mute_write(parse_dump(_mic(False)), MAC, True, 1104) == (None, 1104)
    assert audio_devices.mute_write(parse_dump(_dump(call=False)), MAC, True, None) == (None, None)
    # A stream without Props counts as unmuted.
    assert audio_devices.mute_write(parse_dump(_mic(None)), MAC, False, None) == (None, 1104)


def test_muting_needs_a_call_and_a_boolean() -> None:
    router, pipewire, _loop, _changes = _router()

    with pytest.raises(NotReadyError):
        router.set_muted(True)
    router.calls_changed(True)
    with pytest.raises(InvalidArgumentsError):
        router.set_muted("yes")
    assert _mute_commands(pipewire) == []


def test_muting_writes_the_stream_after_checking_its_id() -> None:
    router, pipewire, _loop, changes = _router(pipewire=FakePipeWire(_mic(False)))
    router.calls_changed(True)
    changes.clear()

    router.set_muted(True)

    assert _mute_commands(pipewire) == [
        ["pw-cli", "set-param", "104", "Props", "{ mute: true }"],
    ]
    assert MIC_STREAM in pipewire.verified
    assert router.call_audio()["muted"] is True and router.muted
    assert changes  # the call view hears about it
    # Settled: a later check does not write again, even if a mixer unmuted it.
    pipewire.dump = _mic(False)
    router._recheck()
    assert len(_mute_commands(pipewire)) == 1


def test_a_recycled_stream_id_is_never_muted() -> None:
    pipewire = FakePipeWire(_mic(False))
    router, pipewire, _loop, _changes = _router(pipewire=pipewire)
    router.calls_changed(True)
    pipewire.verify_dump = _mic(False, serial=4242)

    router.set_muted(True)

    assert _mute_commands(pipewire) == []


def test_a_new_call_stream_gets_the_mute_again() -> None:
    pipewire = FakePipeWire(_mic(False))
    router, pipewire, _loop, _changes = _router(pipewire=pipewire)
    router.calls_changed(True)
    router.set_muted(True)

    # The SCO link came back: a new stream with a new ID and serial.
    pipewire.dump = _mic(False, serial=2104, node_id=204)
    router._recheck()

    assert _mute_commands(pipewire)[-1] == ["pw-cli", "set-param", "204", "Props", "{ mute: true }"]


def test_the_mute_ends_with_the_call() -> None:
    pipewire = FakePipeWire(_mic(False))
    router, pipewire, _loop, _changes = _router(pipewire=pipewire)
    router.calls_changed(True)
    router.set_muted(True)
    pipewire.dump = _mic(True)

    router.calls_changed(False)

    assert _mute_commands(pipewire)[-1] == ["pw-cli", "set-param", "104", "Props", "{ mute: false }"]
    assert router.call_audio()["muted"] is False
    with pytest.raises(NotReadyError):
        router.set_muted(True)


def test_a_mute_wireplumber_restored_is_lifted_on_the_next_call() -> None:
    router, pipewire, _loop, _changes = _router(pipewire=FakePipeWire(_mic(True)))

    router.calls_changed(True)

    assert _mute_commands(pipewire) == [
        ["pw-cli", "set-param", "104", "Props", "{ mute: false }"],
    ]
    assert router.call_audio()["muted"] is False


def test_an_unmuted_call_end_runs_nothing() -> None:
    router, pipewire, _loop, _changes = _router(pipewire=FakePipeWire(_mic(False)))
    router.calls_changed(True)

    router.calls_changed(False)

    assert _mute_commands(pipewire) == []


def test_a_toggle_during_a_check_leaves_the_stream_unsettled() -> None:
    loop = Loop()
    pipewire = FakePipeWire(_mic(False))
    router, pipewire, loop, _changes = _router(pipewire=pipewire, loop=loop)
    router.calls_changed(True)
    loop.defer = True
    router.set_muted(True)
    router.set_muted(False)  # queued behind the running check
    pipewire.dump = _mic(True)
    while loop.deferred:
        loop.deferred.pop(0)()

    # The second toggle is applied, not lost to the first check's result.
    assert _mute_commands(pipewire)[-1][-1] == "{ mute: false }"
    assert router.call_audio()["muted"] is False


def test_switching_calls_off_lifts_the_mute_too() -> None:
    pipewire = FakePipeWire(_mic(False, targets={103: "62"}))
    router, pipewire, _loop, _changes = _router(pipewire=pipewire)
    router.calls_changed(True)
    router.set_muted(True)
    pipewire.dump = _mic(True, targets={103: "62"})

    router.release()

    assert ["pw-cli", "set-param", "104", "Props", "{ mute: false }"] in pipewire.commands


def test_muting_needs_the_call_audio_and_working_tools() -> None:
    router, pipewire, _loop, _changes = _router(pipewire=FakePipeWire(_dump(call=False)))
    router.calls_changed(True)
    with pytest.raises(NotReadyError, match="not there yet"):
        router.set_muted(True)

    broken, pipewire, _loop, _changes = _router(pipewire=FakePipeWire(_mic(False)))
    pipewire.missing = True
    broken.calls_changed(True)
    with pytest.raises(NotReadyError, match="pw-cli"):
        broken.set_muted(True)
    assert _mute_commands(pipewire) == []


def test_reported_mute_follows_pipewire_not_the_request() -> None:
    pipewire = FakePipeWire(_mic(False))
    router, pipewire, _loop, changes = _router(pipewire=pipewire)
    router.calls_changed(True)
    router.set_muted(True)
    assert router.call_audio()["muted"] is True

    # Unmuted by hand in a mixer: the call view says so.
    pipewire.dump = _mic(False)
    changes.clear()
    router._recheck()
    assert router.call_audio()["muted"] is False and changes


def test_a_failed_mute_write_is_undone_and_announced() -> None:
    pipewire = FakePipeWire(_mic(False))
    router, pipewire, _loop, changes = _router(pipewire=pipewire)
    router.calls_changed(True)
    pipewire.metadata_error = True
    changes.clear()

    router.set_muted(True)

    assert router.call_audio()["muted"] is False and router.muted is False
    assert changes  # CallsChanged tells the clients
    pipewire.metadata_error = False
    router._recheck()
    assert _mute_commands(pipewire) == []  # no retry loop
