"""Call audio devices in the backend operations, status, models and daemon wiring."""
from __future__ import annotations

import pytest

from blueferry.backend_operations import BackendDependencies, BackendOperations
from blueferry.calls.settings import CallAudioSettings
from blueferry.dbus_service import MessagesService
from blueferry.errors import (
    CallsDisabledError,
    ConfigurationError,
    InvalidArgumentsError,
    NotReadyError,
)
from blueferry.models import BackendStatus, CallAudio, CallsSnapshot
from blueferry.protocol import ERROR_PREFIX
from blueferry.settings_store import UnreadableSettingsError


class _Sessions:
    map = None
    pbap = None
    map_path = ""

    @staticmethod
    def report_error(_error) -> None:
        pass


class FakeCalls:
    def __init__(self, enabled=True) -> None:
        self.enabled = enabled

    def snapshot(self):
        if not self.enabled:
            return {"calls_enabled": False}
        return {"calls_enabled": True, "calls_state": "ready", "calls_available": True}

    def list_calls(self):
        return {"state": "ready", "calls": []}


class FakeAudio:
    def __init__(self, error=None) -> None:
        self.saved: list[tuple] = []
        self.error = error

    def snapshot(self):
        return {"calls_audio_output": "alsa_output.a50", "calls_audio_input": ""}

    def call_audio(self):
        return {"state": "ready", "output": {"selected": "alsa_output.a50",
                                             "origin": "settings", "device": "alsa_output.a50",
                                             "description": "A50", "stream": True}}

    def list_devices(self, success, _failure):
        success({"outputs": [], "inputs": [], "output": "", "input": ""})

    def set_preferred(self, direction, name):
        if self.error is not None:
            raise self.error
        self.saved.append(("preferred", direction, name))
        return {"calls_audio_output": name}

    def set_for_call(self, direction, name):
        self.saved.append(("call", direction, name))


def _operations(calls=None, audio=None):
    return BackendOperations(_Sessions(), BackendDependencies(
        calls=calls if calls is not None else FakeCalls(), call_audio=audio,
    ))


def test_status_reports_the_saved_devices_only_while_calls_are_on() -> None:
    on = _operations(audio=FakeAudio()).status()
    off = _operations(calls=FakeCalls(enabled=False), audio=FakeAudio()).status()

    assert on["calls_audio_output"] == "alsa_output.a50" and on["calls_audio_input"] == ""
    assert not any(key.startswith("calls_audio") for key in off)
    assert "calls_audio_output" not in _operations().status()


def test_list_calls_carries_the_call_audio() -> None:
    payload = _operations(audio=FakeAudio()).list_calls()

    assert payload["audio"]["output"]["description"] == "A50"
    assert "audio" not in _operations().list_calls()


def test_setters_need_calls_and_the_capability() -> None:
    audio = FakeAudio()
    operations = _operations(audio=audio)

    assert operations.set_call_audio_device("output", "x") == {"calls_audio_output": "x"}
    operations.set_call_audio_device_for_call("input", "")
    assert audio.saved == [("preferred", "output", "x"), ("call", "input", "")]

    with pytest.raises(CallsDisabledError):
        _operations(calls=FakeCalls(enabled=False), audio=audio).set_call_audio_device("output", "")
    with pytest.raises(NotReadyError):
        _operations().set_call_audio_device_for_call("output", "")
    replies: list[dict] = []
    operations.list_call_audio_devices(replies.append, lambda _error: None)
    assert replies[0]["outputs"] == []


@pytest.mark.parametrize(("error", "expected"), [
    (ValueError("bad"), InvalidArgumentsError),
    (OSError("disk full"), NotReadyError),
    # Also a ValueError, but not the caller's fault.
    (UnreadableSettingsError("settings.json could not be read"), ConfigurationError),
])
def test_save_failures_are_typed(error, expected) -> None:
    with pytest.raises(expected):
        _operations(audio=FakeAudio(error=error)).set_call_audio_device("output", "x")


def test_unreadable_settings_refuse_the_device_as_a_configuration_error(tmp_path) -> None:
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")
    path.chmod(0o600)

    class SavingAudio(FakeAudio):
        def set_preferred(self, direction, name):
            CallAudioSettings(path).set(direction, name)
            return {"calls_audio_output": name}

    with pytest.raises(ConfigurationError) as caught:
        _operations(audio=SavingAudio()).set_call_audio_device("output", "alsa_output.a50")

    error = MessagesService._dbus_error(caught.value)
    assert error.get_dbus_name() == f"{ERROR_PREFIX}.Configuration"
    assert error.get_dbus_message().startswith(
        "settings.json could not be read, so the change was not saved"
    )
    assert path.read_text(encoding="utf-8") == "{not json"


def test_models_read_the_call_audio_and_keep_empty_choices() -> None:
    status = BackendStatus.from_dict({"calls_audio_output": "", "calls_audio_input": "mic"})
    assert (status.calls_audio_output, status.calls_audio_input) == ("", "mic")
    assert status.to_dict()["calls_audio_output"] == ""
    assert "calls_audio_output" not in BackendStatus.from_dict({}).to_dict()

    snapshot = CallsSnapshot.from_dict({"state": "ready", "calls": [], "audio": {
        "state": "ready", "output": {"selected": "a", "origin": "call", "missing": True},
    }})
    assert isinstance(snapshot.audio, CallAudio)
    assert snapshot.audio.output.missing and snapshot.audio.output.origin == "call"
    assert snapshot.audio.input.origin == "system"
    assert CallAudio.from_dict(snapshot.audio.to_dict()) == snapshot.audio
    assert CallsSnapshot.from_dict({"state": "ready"}).audio is None


def test_daemon_routes_call_audio_only_while_a_call_runs(make_daemon) -> None:
    instance = make_daemon()
    seen: list[tuple[bool, bool, bool]] = []
    instance.call_audio.calls_changed = (
        lambda in_call, *, active, answered_here: seen.append((in_call, active, answered_here))
    )
    emitted: list[str] = []
    instance._emit_calls_changed = lambda: emitted.append("calls")

    instance._calls_changed()

    assert seen == [(False, False, False)] and emitted == ["calls"]
    status = BackendOperations(_Sessions(), BackendDependencies(
        calls=instance.calls, call_audio=instance.call_audio,
    )).status()
    assert "calls_audio_output" not in status  # calls are off by default


def test_switching_calls_off_releases_the_call_stream_targets(make_daemon, monkeypatch) -> None:
    instance = make_daemon()
    order: list[str] = []
    instance.call_audio.release = lambda: order.append("release")
    instance._emit_status = lambda: None
    monkeypatch.setattr(instance.calls_settings, "set", lambda enabled: enabled)
    monkeypatch.setattr(
        instance.calls, "set_enabled", lambda enabled: order.append(f"calls {enabled}"),
    )

    instance._set_calls_enabled(True)
    instance._set_calls_enabled(False)

    # The release is queued before the hands-free link (and the streams) go.
    assert order == ["calls True", "release", "calls False"]
