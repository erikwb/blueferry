"""Wording of the call speaker/microphone choice shared by every client."""
from __future__ import annotations

from blueferry import call_audio_view as view
from blueferry.models import CallAudio

DEVICES = {
    "outputs": [
        {"name": "alsa_output.a50", "description": "A50 Chat", "default": False},
        {"name": "alsa_output.builtin", "description": "Built-in Audio", "default": True},
        {"name": "alsa_output.a50", "description": "duplicate", "default": False},
        "garbage",
    ],
    "inputs": [{"name": "alsa_input.a50", "description": "", "default": False}],
}


def test_support_follows_the_status_key() -> None:
    assert view.supported({"calls_audio_output": ""})
    assert not view.supported({"calls_enabled": True})
    assert view.saved({"calls_audio_input": "mic"}, "input") == "mic"
    assert view.saved({}, "output") == ""


def test_options_start_with_the_system_and_name_its_device() -> None:
    choices = view.options(DEVICES, "output", "")

    assert choices == [
        {"value": "", "label": "Same as system (Built-in Audio)"},
        {"value": "alsa_output.a50", "label": "A50 Chat"},
        {"value": "alsa_output.builtin", "label": "Built-in Audio"},
    ]
    assert view.selected_index(choices, "alsa_output.builtin") == 2
    assert view.selected_index(choices, "unknown") == 0


def test_a_disconnected_choice_stays_visible() -> None:
    choices = view.options(DEVICES, "input", "alsa_input.gone")

    assert choices[0] == {"value": "", "label": "Same as system"}
    assert choices[1] == {"value": "alsa_input.a50", "label": "alsa_input.a50"}
    assert choices[-1] == {"value": "alsa_input.gone", "label": "alsa_input.gone (not connected)"}
    assert view.options(None, "output", "") == [{"value": "", "label": "Same as system"}]


def test_call_view_names_the_devices_in_use() -> None:
    ready = view.call_audio({"state": "ready", "output": {
        "selected": "alsa_output.a50", "origin": "settings", "device": "alsa_output.a50",
        "description": "A50 Chat", "stream": True,
    }, "input": {"stream": True, "description": "Webcam", "device": "alsa_input.cam"}})

    assert ready["supported"] and ready["hint"] == ""
    assert ready["rows"][0]["using"] == "In use: A50 Chat"
    assert [(row["label"], row["text"], row["selected"]) for row in ready["rows"]] == [
        ("Speaker", "A50 Chat", "alsa_output.a50"), ("Microphone", "Webcam", ""),
    ]


def test_call_view_explains_waiting_missing_and_unavailable() -> None:
    assert not view.call_audio(None)["supported"]
    waiting = view.call_audio(CallAudio(state="waiting"))
    assert waiting["hint"] == "Waiting for the call audio…"
    assert waiting["rows"][0]["text"] == "Not connected yet"

    missing = view.call_audio({"state": "ready", "output": {
        "selected": "alsa_output.gone", "missing": True, "stream": True,
        "description": "Built-in Audio",
    }, "input": {"stream": True, "description": "A50"}})
    assert "speaker is not connected" in missing["hint"]
    assert missing["rows"][0]["text"] == "Built-in Audio"
    both = view.call_audio({"state": "ready", "output": {"missing": True},
                            "input": {"missing": True}})
    assert "devices are not connected" in both["hint"]
    mic = view.call_audio({"state": "ready", "input": {"missing": True}})
    assert "microphone is not connected" in mic["hint"]

    unavailable = view.call_audio({"state": "unavailable"})
    assert unavailable["rows"][1]["text"] == "System default"
    assert "Could not read" in unavailable["hint"]


def test_settings_view_has_one_row_per_direction() -> None:
    result = view.settings_view({"calls_audio_output": "alsa_output.builtin"}, DEVICES)

    assert result["supported"] is True
    assert result["hint"].startswith("Used only for phone calls")
    output, input_ = result["rows"]
    assert (output["label"], output["index"]) == ("Speaker for calls", 2)
    assert (input_["label"], input_["index"], input_["selected"]) == ("Microphone for calls", 0, "")
    assert view.settings_view({}, None)["supported"] is False


def test_call_view_offers_the_switch_options() -> None:
    result = view.call_view({"state": "ready", "output": {
        "selected": "alsa_output.a50", "origin": "call", "stream": True, "description": "A50",
    }}, DEVICES)

    assert result["call_hint"] == "Applies to this call only."
    assert result["rows"][0]["index"] == 1
    assert result["rows"][1]["options"][0]["value"] == ""
    assert view.call_view(None, DEVICES)["rows"] == []
