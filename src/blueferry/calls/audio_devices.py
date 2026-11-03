"""Choose the speaker and microphone for phone-call audio.

With ``bluez5.hfphsp-backend = "ofono"`` PipeWire represents a running call
as two *streams*, not devices (observed with an iPhone, PipeWire 1.6.9,
WirePlumber 0.5.18): ``bluez_input.<MAC>.*`` (``Stream/Output/Audio``, the
caller's voice) is linked to a sink, and ``bluez_output.<MAC>.*``
(``Stream/Input/Audio``, this computer's voice) is linked from a source.
WirePlumber links both to the system defaults. They appear only once the
SCO link is up, a few seconds after answering or dialing, and get new node
IDs for every call; their ``node.name`` stays the same.

A stream is moved without touching the system defaults by writing
``target.object`` (the target's ``object.serial``) for the stream's node ID
into the ``default`` metadata, which WirePlumber's
``linking/find-defined-target.lua`` honours. "Same as system" deletes that
key, so WirePlumber relinks the stream to the default.

WirePlumber's ``node/state-stream.lua`` (``node.stream.restore-target``, on
by default) saves such a metadata target under a key built from the stream's
own properties (its application, media name or node name), so only the call
streams of that phone are remembered, and restores it on the next call. The
router therefore checks the target on every call and deletes a remembered
target when the choice is "same as system"; a deletion also clears what
WirePlumber saved. Other applications' streams and the system defaults are
never written.

Everything that runs ``pw-dump``/``pw-metadata`` happens on a worker; the
planning functions below are pure, so tests feed them recorded dumps.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from blueferry.errors import CommandError, InvalidArgumentsError, NotReadyError

log = logging.getLogger(__name__)

OUTPUT = "output"
INPUT = "input"
DIRECTIONS = (OUTPUT, INPUT)

SINK_CLASS = "Audio/Sink"
SOURCE_CLASS = "Audio/Source"
DEVICE_CLASS = {OUTPUT: SINK_CLASS, INPUT: SOURCE_CLASS}
# The phone's call streams: what the caller says plays on a sink (output);
# what this computer records goes to the phone (input).
STREAM_CLASS = {OUTPUT: "Stream/Output/Audio", INPUT: "Stream/Input/Audio"}
STREAM_PREFIX = {OUTPUT: "bluez_input.", INPUT: "bluez_output."}

TARGET_KEY = "target.object"
DEFAULT_METADATA = "default"
DEFAULT_KEY = {OUTPUT: "default.audio.sink", INPUT: "default.audio.source"}
NODE_TYPE = "PipeWire:Interface:Node"
LINK_TYPE = "PipeWire:Interface:Link"
METADATA_TYPE = "PipeWire:Interface:Metadata"

MAX_NODE_NAME = 255
MAX_DESCRIPTION = 255
MAX_DEVICES = 64
PW_DUMP_TIMEOUT_SEC = 5.0
PW_METADATA_TIMEOUT_SEC = 3.0

# Where a choice for this call came from (``origin`` in ListCalls).
ORIGIN_CALL = "call"
ORIGIN_SETTINGS = "settings"
ORIGIN_SYSTEM = "system"

# ListCalls ``audio.state``.
STATE_IDLE = "idle"            # no call
STATE_WAITING = "waiting"      # a call, but no call audio in PipeWire yet
STATE_READY = "ready"          # at least one call stream found
STATE_UNAVAILABLE = "unavailable"  # pw-dump missing or failing

# The call streams appear a few seconds after the SCO link comes up. Check
# every second for a while, then keep an eye on hot-plugged devices.
FAST_INTERVAL_SEC = 1
FAST_CHECKS = 20
STEADY_INTERVAL_SEC = 5
# After a move, look again soon so the call view shows the new device.
FOLLOW_UP_CHECKS = 3
# A failed pw-dump counts as "unavailable" only when the next one fails too
# (or at once when the tool is missing).
UNAVAILABLE_AFTER_FAILURES = 2

Run = Callable[..., Any]
Submit = Callable[..., None]
Schedule = Callable[[int, Callable[[], bool]], int]
Cancel = Callable[[int], object]


def valid_node_name(value: object) -> str:
    """A saved device: a PipeWire ``node.name`` or "" for the system default."""
    if not isinstance(value, str):
        raise InvalidArgumentsError("audio device must be a string")
    name = value.strip()
    if len(name) > MAX_NODE_NAME or any(ord(char) < 0x20 or ord(char) == 0x7F for char in name):
        raise InvalidArgumentsError("audio device name is invalid")
    return name


def valid_direction(value: object) -> str:
    if value not in DIRECTIONS:
        raise InvalidArgumentsError("audio direction must be 'output' or 'input'")
    return str(value)


@dataclass(frozen=True, slots=True)
class Node:
    id: int
    serial: int
    name: str
    description: str
    media_class: str
    address: str = ""


@dataclass(frozen=True, slots=True)
class Graph:
    nodes: Mapping[int, Node] = field(default_factory=dict)
    # (output node, input node) for every link.
    links: tuple[tuple[int, int], ...] = ()
    # ``target.object`` values in the default metadata, by subject node ID.
    targets: Mapping[int, str] = field(default_factory=dict)
    # node.name of the current default sink/source, by direction.
    defaults: Mapping[str, str] = field(default_factory=dict)


def _int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(char for char in value if ord(char) >= 0x20 and ord(char) != 0x7F)[:limit]


def parse_dump(text: str) -> Graph:
    """The parts of ``pw-dump`` output the router needs. Never raises on content."""
    try:
        objects = json.loads(text)
    except (TypeError, ValueError):
        return Graph()
    if not isinstance(objects, list):
        return Graph()
    nodes: dict[int, Node] = {}
    links: list[tuple[int, int]] = []
    targets: dict[int, str] = {}
    defaults: dict[str, str] = {}
    for item in objects:
        if not isinstance(item, Mapping):
            continue
        kind = item.get("type")
        info = _mapping(item.get("info"))
        if kind == NODE_TYPE:
            props = _mapping(info.get("props"))
            node_id, serial = _int(item.get("id")), _int(props.get("object.serial"))
            name = _text(props.get("node.name"), MAX_NODE_NAME)
            if node_id is None or serial is None or not name:
                continue
            nodes[node_id] = Node(
                id=node_id, serial=serial, name=name,
                description=_text(props.get("node.description"), MAX_DESCRIPTION) or name,
                media_class=_text(props.get("media.class"), 64),
                address=_text(props.get("api.bluez5.address"), 32).upper(),
            )
        elif kind == LINK_TYPE:
            output, input_ = _int(info.get("output-node-id")), _int(info.get("input-node-id"))
            if output is not None and input_ is not None:
                links.append((output, input_))
        elif kind == METADATA_TYPE:
            props = _mapping(item.get("props"))
            if props.get("metadata.name") != DEFAULT_METADATA:
                continue
            entries = item.get("metadata")
            for entry in entries if isinstance(entries, list) else ():
                if not isinstance(entry, Mapping):
                    continue
                subject, key = _int(entry.get("subject")), entry.get("key")
                value = entry.get("value")
                if key == TARGET_KEY and subject is not None and value is not None:
                    targets[subject] = str(value)
                for direction, default_key in DEFAULT_KEY.items():
                    if subject == 0 and key == default_key:
                        defaults[direction] = _default_name(value)
    return Graph(nodes=nodes, links=tuple(links), targets=targets, defaults=defaults)


def _default_name(value: object) -> str:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return ""
    if isinstance(value, Mapping):
        return _text(value.get("name"), MAX_NODE_NAME)
    return ""


def _mac_token(mac: str) -> str:
    return mac.strip().upper().replace(":", "_")


def _belongs_to_phone(node: Node, mac: str) -> bool:
    phone = mac.strip().upper()
    return bool(phone) and (node.address == phone or _mac_token(mac) in node.name.upper())


def devices(graph: Graph, direction: str, mac: str) -> list[Node]:
    """Speakers (sinks) or microphones (sources) a call with ``mac`` can use.

    The phone's own nodes (its media source, its call streams) are left out;
    other Bluetooth headsets are offered. Monitors are not separate nodes in
    PipeWire, and the exact media classes leave out virtual and internal ones.
    """
    wanted = DEVICE_CLASS[direction]
    found = [
        node for node in graph.nodes.values()
        if node.media_class == wanted and not node.name.endswith(".monitor")
        and not _belongs_to_phone(node, mac)
    ]
    found.sort(key=lambda node: (node.description.casefold(), node.name))
    return found[:MAX_DEVICES]


def call_stream(graph: Graph, mac: str, direction: str) -> Node | None:
    """The phone's call stream for one direction, bound by the phone's address."""
    wanted, prefix = STREAM_CLASS[direction], STREAM_PREFIX[direction]
    for node in sorted(graph.nodes.values(), key=lambda item: item.id):
        if (
            node.media_class == wanted and node.name.startswith(prefix)
            and _belongs_to_phone(node, mac)
        ):
            return node
    return None


def linked_device(graph: Graph, stream: Node, direction: str) -> Node | None:
    """Where the stream is linked right now (the device actually used)."""
    peers = sorted(
        input_ if direction == OUTPUT else output
        for output, input_ in graph.links
        if (output if direction == OUTPUT else input_) == stream.id
    )
    for peer in peers:
        node = graph.nodes.get(peer)
        if node is not None:
            return node
    return None


def find_device(graph: Graph, direction: str, name: str, mac: str) -> Node | None:
    if not name:
        return None
    for node in devices(graph, direction, mac):
        if node.name == name:
            return node
    return None


def set_target_argv(stream: Node, target: Node) -> list[str]:
    return [
        "pw-metadata", "-n", DEFAULT_METADATA, str(stream.id), TARGET_KEY,
        str(target.serial), "Spa:Id",
    ]


def clear_target_argv(stream: Node) -> list[str]:
    return ["pw-metadata", "-n", DEFAULT_METADATA, "-d", str(stream.id), TARGET_KEY]


@dataclass(frozen=True, slots=True)
class MetadataWrite:
    """One ``pw-metadata`` call for one call stream."""

    stream: Node
    argv: list[str]


@dataclass(frozen=True, slots=True)
class Choice:
    """What to use for one direction during this call."""

    name: str  # node.name, or "" for the system default
    origin: str  # ORIGIN_CALL, ORIGIN_SETTINGS or ORIGIN_SYSTEM


def plan(
    graph: Graph, mac: str, choices: Mapping[str, Choice],
) -> tuple[list[MetadataWrite], dict[str, dict[str, Any]]]:
    """Writes that apply ``choices`` and the per-direction call audio report.

    BlueFerry owns the call streams' target while a call runs: with "same
    as system" any ``target.object`` is deleted, also one set by hand in a
    mixer such as pavucontrol or Helvum.
    """
    commands: list[MetadataWrite] = []
    report: dict[str, dict[str, Any]] = {}
    for direction in DIRECTIONS:
        choice = choices.get(direction, Choice("", ORIGIN_SYSTEM))
        stream = call_stream(graph, mac, direction)
        target = find_device(graph, direction, choice.name, mac)
        entry: dict[str, Any] = {
            "selected": choice.name, "origin": choice.origin,
            "missing": bool(choice.name) and target is None,
            "device": "", "description": "",
        }
        if stream is not None:
            current = graph.targets.get(stream.id)
            if target is not None:
                if current != str(target.serial):
                    commands.append(MetadataWrite(stream, set_target_argv(stream, target)))
            elif current is not None:
                commands.append(MetadataWrite(stream, clear_target_argv(stream)))
            used = linked_device(graph, stream, direction)
            if used is not None:
                entry["device"], entry["description"] = used.name, used.description
        entry["stream"] = stream is not None
        report[direction] = entry
    return commands, report


def device_list(graph: Graph, mac: str) -> dict[str, Any]:
    """The ListCallAudioDevices payload, without the saved choices."""
    result: dict[str, Any] = {}
    for direction, key in ((OUTPUT, "outputs"), (INPUT, "inputs")):
        default = graph.defaults.get(direction, "")
        result[key] = [
            {"name": node.name, "description": node.description, "default": node.name == default}
            for node in devices(graph, direction, mac)
        ]
    return result


def read_graph(run: Run) -> Graph:
    """Run ``pw-dump`` (blocking; worker only)."""
    result = run(["pw-dump"], timeout=PW_DUMP_TIMEOUT_SEC)
    return parse_dump(result.stdout)


def still_there(run: Run, stream: Node) -> bool:
    """Whether ``stream``'s node ID still names the same stream.

    PipeWire reuses global IDs. A stream that ended between the full dump
    and the write could hand its ID to another node, so each write first
    reads that one object again and compares ``object.serial`` (never
    reused) and ``node.name``.
    """
    result = run(["pw-dump", str(stream.id)], timeout=PW_DUMP_TIMEOUT_SEC)
    node = parse_dump(result.stdout).nodes.get(stream.id)
    return node is not None and node.serial == stream.serial and node.name == stream.name


def apply_writes(run: Run, writes: list[MetadataWrite]) -> tuple[int, int]:
    """Run the writes whose stream is still there: (applied, failed). Worker only."""
    applied = failed = 0
    for write in writes:
        try:
            if not still_there(run, write.stream):
                log.debug("call stream %d went away before its target was written",
                          write.stream.id)
                continue
            run(write.argv, timeout=PW_METADATA_TIMEOUT_SEC)
        except CommandError as error:
            failed += 1
            log.debug("could not write a call stream target: %s", error)
        else:
            applied += 1
    return applied, failed


def release_targets(run: Run, mac: str) -> int:
    """Delete any ``target.object`` of the phone's call streams (worker only).

    Run when calls are switched off, so the target WirePlumber remembers
    for the phone's streams does not outlive the feature.
    """
    graph = read_graph(run)
    writes = [
        MetadataWrite(stream, clear_target_argv(stream))
        for direction in DIRECTIONS
        if (stream := call_stream(graph, mac, direction)) is not None
        and stream.id in graph.targets
    ]
    applied, _failed = apply_writes(run, writes)
    return applied


class CallAudioRouter:
    """Apply the saved or per-call speaker/microphone while a call runs.

    Inert without a call: nothing runs until the call controller reports
    one. Every ``pw-*`` command runs through ``submit`` (a worker); results
    arrive back on the main loop. A generation drops results that belong to
    a call that has already ended.
    """

    def __init__(
        self,
        *,
        settings: Any,
        mac: str,
        submit: Submit,
        run: Run,
        schedule: Schedule,
        cancel: Cancel,
        on_changed: Callable[[], None] | None = None,
    ) -> None:
        self._settings = settings
        self._mac = mac
        self._submit = submit
        self._run = run
        self._schedule = schedule
        self._cancel = cancel
        self._on_changed = on_changed or (lambda: None)
        self._in_call = False
        self._generation = 0
        self._overrides: dict[str, str] = {}
        self._report: dict[str, dict[str, Any]] = {}
        self._state = STATE_IDLE
        self._timer: int | None = None
        self._busy = False
        self._again = False
        self._fast_left = 0
        self._failures = 0

    # ---- status ---------------------------------------------------------

    def snapshot(self) -> dict[str, str]:
        """GetStatus keys while calls are on: the saved choices."""
        return {
            "calls_audio_output": self._settings.get(OUTPUT),
            "calls_audio_input": self._settings.get(INPUT),
        }

    def choices(self) -> dict[str, Choice]:
        result: dict[str, Choice] = {}
        for direction in DIRECTIONS:
            if direction in self._overrides:
                result[direction] = Choice(self._overrides[direction], ORIGIN_CALL)
            elif self._settings.get(direction):
                result[direction] = Choice(self._settings.get(direction), ORIGIN_SETTINGS)
            else:
                result[direction] = Choice("", ORIGIN_SYSTEM)
        return result

    def call_audio(self) -> dict[str, Any]:
        """The ``audio`` part of ListCalls."""
        audio: dict[str, Any] = {"state": self._state}
        choices = self.choices()
        for direction in DIRECTIONS:
            entry = dict(self._report.get(direction) or {})
            choice = choices[direction]
            entry.setdefault("device", "")
            entry.setdefault("description", "")
            entry.setdefault("stream", False)
            entry.setdefault("missing", False)
            entry["selected"], entry["origin"] = choice.name, choice.origin
            audio[direction] = entry
        return audio

    # ---- configuration --------------------------------------------------

    def set_preferred(self, direction: object, name: object) -> dict[str, str]:
        direction, name = valid_direction(direction), valid_node_name(name)
        self._settings.set(direction, name)
        if self._in_call:
            self._recheck()
        return self.snapshot()

    def set_for_call(self, direction: object, name: object) -> None:
        direction, name = valid_direction(direction), valid_node_name(name)
        if not self._in_call:
            raise NotReadyError("no call is in progress")
        self._overrides[direction] = name
        self._recheck()

    def list_devices(
        self, success: Callable[[dict[str, Any]], None], failure: Callable[[Exception], None],
    ) -> None:
        run, mac = self._run, self._mac

        def done(graph: object) -> None:
            if not isinstance(graph, Graph):
                failure(NotReadyError("PipeWire did not answer"))
                return
            payload = device_list(graph, mac)
            payload[OUTPUT] = self._settings.get(OUTPUT)
            payload[INPUT] = self._settings.get(INPUT)
            success(payload)

        def failed(error: Exception) -> None:
            log.info("could not list audio devices: %s", error)
            failure(NotReadyError("could not read PipeWire's devices (is pw-dump installed?)"))

        try:
            self._submit(lambda: read_graph(run), on_success=done, on_error=failed)
        except RuntimeError as error:
            raise NotReadyError("PipeWire is still being read; try again") from error

    # ---- call lifecycle -------------------------------------------------

    def calls_changed(self, in_call: bool) -> None:
        in_call = bool(in_call)
        if in_call and self._in_call:
            # Another change during a call (answered, a second call): the
            # call audio may appear now, so look every second again.
            self._fast_left = max(self._fast_left, FAST_CHECKS)
            if self._timer is not None:
                self._cancel_timer()
                self._arm()
            return
        if in_call == self._in_call:
            return
        self._in_call = in_call
        self._generation += 1
        self._busy = False
        self._again = False
        self._cancel_timer()
        # The caller announces the call change itself (CallsChanged), so the
        # state moves here without another notification.
        self._report = {}
        self._failures = 0
        if not in_call:
            # A choice made during a call ends with the call.
            self._overrides.clear()
            self._state = STATE_IDLE
            return
        self._fast_left = FAST_CHECKS
        self._state = STATE_WAITING
        self._check()

    def stop(self) -> None:
        self.calls_changed(False)

    def _recheck(self) -> None:
        self._fast_left = max(self._fast_left, FOLLOW_UP_CHECKS)
        self._cancel_timer()
        self._check()

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            try:
                self._cancel(self._timer)
            except Exception:
                log.debug("could not cancel the call audio timer", exc_info=True)
            self._timer = None

    def _arm(self) -> None:
        if not self._in_call or self._timer is not None:
            return
        fast = self._fast_left > 0 and self._state != STATE_UNAVAILABLE
        delay = FAST_INTERVAL_SEC if fast else STEADY_INTERVAL_SEC
        self._timer = self._schedule(delay, self._tick)

    def _tick(self) -> bool:
        self._timer = None
        self._check()
        return False

    def _check(self) -> None:
        if not self._in_call:
            return
        if self._busy:
            self._again = True
            return
        self._busy = True
        generation = self._generation
        choices, mac, run = self.choices(), self._mac, self._run

        def job() -> tuple[dict[str, dict[str, Any]], int, int]:
            graph = read_graph(run)
            writes, report = plan(graph, mac, choices)
            applied, failed = apply_writes(run, writes)
            return report, applied, failed

        try:
            self._submit(
                job,
                on_success=lambda value: self._checked(generation, value),
                on_error=lambda error: self._failed(generation, error),
            )
        except Exception:
            log.warning("could not check call audio", exc_info=True)
            self._busy = False
            self._arm()

    def _checked(self, generation: int, value: object) -> None:
        if generation != self._generation:
            return
        self._busy = False
        self._failures = 0
        report, applied, failed = value if isinstance(value, tuple) else ({}, 0, 0)
        found = any(entry.get("stream") for entry in report.values())
        if failed:
            # A write failed (the stream may be going away): look again soon.
            self._fast_left = max(self._fast_left, FOLLOW_UP_CHECKS)
        elif applied:
            log.info("moved the call audio to the chosen device (%d change(s))", applied)
            self._fast_left = FOLLOW_UP_CHECKS
        elif report and all(entry.get("stream") for entry in report.values()):
            self._fast_left = 0
        else:
            self._fast_left = max(self._fast_left - 1, 0)
        changed = report != self._report
        self._report = dict(report)
        state = STATE_READY if found else STATE_WAITING
        if state != self._state:
            self._set_state(state)
        elif changed:
            self._notify()
        self._continue()

    def _failed(self, generation: int, error: Exception) -> None:
        if generation != self._generation:
            return
        self._busy = False
        self._failures += 1
        missing = "is not installed" in str(error)
        if not missing and self._failures < UNAVAILABLE_AFTER_FAILURES:
            # One slow or failed pw-dump is not news: keep the last report
            # and look again in a second.
            log.debug("could not read PipeWire for the call audio: %s", error)
            self._fast_left = max(self._fast_left, 1)
            self._continue()
            return
        # Logged once per call; pw-dump may simply not be installed.
        if self._state != STATE_UNAVAILABLE:
            log.warning("could not route the call audio: %s", error)
        self._report = {}
        self._set_state(STATE_UNAVAILABLE)
        self._continue()

    def release(self) -> None:
        """Calls were switched off: delete the call streams' targets, if any."""
        run, mac = self._run, self._mac

        def failed(error: Exception) -> None:
            log.debug("could not release the call stream targets: %s", error)

        try:
            self._submit(
                lambda: release_targets(run, mac), on_success=lambda _count: None,
                on_error=failed,
            )
        except RuntimeError:
            log.debug("call audio worker busy; targets not released", exc_info=True)

    def _continue(self) -> None:
        if self._again:
            self._again = False
            self._check()
            return
        self._arm()

    def _set_state(self, state: str) -> None:
        if state == self._state:
            return
        self._state = state
        self._notify()

    def _notify(self) -> None:
        try:
            self._on_changed()
        except Exception:
            log.exception("call audio change callback failed")

