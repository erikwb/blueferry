"""Toolkit-free wording for the call speaker and microphone, shared by every client.

Settings use the GetStatus keys ``calls_audio_output``/``calls_audio_input``
(the saved PipeWire ``node.name``, "" for the system default) and the
``Calls1.ListCallAudioDevices`` lists. The running call uses the ListCalls
``audio`` part (``CallAudio``). Every string returned here is plain text;
each client escapes it for its own renderer.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from blueferry.i18n import _
from blueferry.models import CallAudio

OUTPUT = "output"
INPUT = "input"
DIRECTIONS = (OUTPUT, INPUT)
STATUS_KEYS = {OUTPUT: "calls_audio_output", INPUT: "calls_audio_input"}
_LIST_KEYS = {OUTPUT: "outputs", INPUT: "inputs"}


def supported(status: Mapping[str, Any]) -> bool:
    """Whether the backend offers the choice (calls on, new enough daemon)."""
    return STATUS_KEYS[OUTPUT] in status


def label(direction: str) -> str:
    return _("Speaker") if direction == OUTPUT else _("Microphone")


def settings_label(direction: str) -> str:
    return _("Speaker for calls") if direction == OUTPUT else _("Microphone for calls")


def settings_hint() -> str:
    return _("Used only for phone calls; everything else keeps the system's devices.")


def call_hint() -> str:
    return _("Applies to this call only.")


def mute_label(muted: bool) -> str:
    """The mute button: what a press does."""
    return _("Unmute") if muted else _("Mute")


def mute_hint(muted: bool) -> str:
    return _("Your microphone is muted for this call.") if muted else _(
        "Mutes this computer's microphone for this call only."
    )


def mute(value: CallAudio | Mapping[str, Any] | None) -> dict[str, Any]:
    """The mute button of the call view.

    ``supported`` is False for daemons that cannot mute a call; ``muted``
    is the current state, ``label`` names the action (Mute/Unmute) and
    ``hint`` explains it (a tooltip or a status line). ``available`` is
    True once the call's microphone stream exists: the daemon refuses to
    mute before that, so clients offer the button only then.
    """
    audio = value if isinstance(value, CallAudio) else CallAudio.from_dict(value)
    if audio is None or audio.muted is None:
        return {
            "supported": False, "available": False, "muted": False,
            "label": mute_label(False), "hint": "",
        }
    return {
        "supported": True, "available": audio.state == "ready" and audio.input.stream,
        "muted": audio.muted, "label": mute_label(audio.muted), "hint": mute_hint(audio.muted),
    }


def mute_failure_text(error: object) -> str:
    """A refused or failed mute; the daemon's text says why."""
    detail = str(error).strip()
    return detail or _("Could not change the microphone for this call.")


def saved(status: Mapping[str, Any], direction: str) -> str:
    value = status.get(STATUS_KEYS[direction])
    return value if isinstance(value, str) else ""


def options(
    devices: Mapping[str, Any] | None, direction: str, selected: str,
) -> list[dict[str, str]]:
    """Choices for one direction: "Same as system", then every device.

    A chosen device that is not connected stays in the list, marked, so
    picking another one is a visible change rather than a silent reset.
    Each entry has ``value`` (the node.name to send, "" for the system) and
    ``label``.
    """
    items = devices.get(_LIST_KEYS[direction]) if isinstance(devices, Mapping) else None
    entries = [item for item in items if isinstance(item, Mapping)] if isinstance(
        items, Sequence,
    ) else []
    default = next(
        (str(item.get("description") or item.get("name") or "") for item in entries
         if item.get("default")),
        "",
    )
    system = (
        _("Same as system ({device})").format(device=default) if default
        else _("Same as system")
    )
    result = [{"value": "", "label": system}]
    names = set()
    for item in entries:
        name = item.get("name")
        if not isinstance(name, str) or not name or name in names:
            continue
        names.add(name)
        result.append({"value": name, "label": str(item.get("description") or name)})
    if selected and selected not in names:
        result.append({
            "value": selected,
            "label": _("{device} (not connected)").format(device=selected),
        })
    return result


def selected_index(choices: Sequence[Mapping[str, str]], selected: str) -> int:
    for index, choice in enumerate(choices):
        if choice.get("value") == selected:
            return index
    return 0


def call_audio(value: CallAudio | Mapping[str, Any] | None) -> dict[str, Any]:
    """The running call's speaker and microphone, for the call view.

    ``supported`` is False for daemons without the ``audio`` part.
    ``rows`` holds one entry per direction: ``direction``, ``label``,
    ``text`` (the device actually used), ``using`` (the same as a sentence),
    ``selected`` (the node.name the switch should show) and ``missing``.
    ``hint`` explains a waiting or missing device and is empty otherwise.
    ``mute`` is the mute button (see ``mute``).
    """
    audio = value if isinstance(value, CallAudio) else CallAudio.from_dict(value)
    if audio is None:
        return {"supported": False, "state": "", "rows": [], "hint": "", "mute": mute(None)}
    rows = []
    missing = []
    for direction in DIRECTIONS:
        route = audio.output if direction == OUTPUT else audio.input
        if route.stream and route.description:
            text = route.description
        elif audio.state == "unavailable":
            text = _("System default")
        else:
            text = _("Not connected yet")
        rows.append({
            "direction": direction, "label": label(direction), "text": text,
            "using": _("In use: {device}").format(device=text),
            "selected": route.selected, "missing": route.missing,
        })
        if route.missing:
            missing.append(direction)
    if audio.state == "unavailable":
        hint = _("Could not read the audio devices; the call uses the system's devices.")
    elif audio.state == "waiting":
        hint = _("Waiting for the call audio…")
    elif missing == [OUTPUT]:
        hint = _("The chosen speaker is not connected; the call uses the system default.")
    elif missing == [INPUT]:
        hint = _("The chosen microphone is not connected; the call uses the system default.")
    elif missing:
        hint = _("The chosen devices are not connected; the call uses the system defaults.")
    else:
        hint = ""
    return {
        "supported": True, "state": audio.state, "rows": rows, "hint": hint,
        "mute": mute(audio),
    }


def settings_view(
    status: Mapping[str, Any], devices: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """The settings rows: one choice per direction with its options and index."""
    rows = []
    for direction in DIRECTIONS:
        selected = saved(status, direction)
        choices = options(devices, direction, selected)
        rows.append({
            "direction": direction, "label": settings_label(direction),
            "selected": selected, "options": choices,
            "index": selected_index(choices, selected),
        })
    return {"supported": supported(status), "hint": settings_hint(), "rows": rows}


def call_view(
    value: CallAudio | Mapping[str, Any] | None, devices: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """``call_audio`` plus the options of the per-call switch in each row."""
    result = call_audio(value)
    for row in result["rows"]:
        choices = options(devices, row["direction"], row["selected"])
        row["options"] = choices
        row["index"] = selected_index(choices, row["selected"])
    result["call_hint"] = call_hint()
    return result
