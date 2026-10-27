from __future__ import annotations

import base64
import io
import json
import threading
import time
from types import SimpleNamespace

import pytest

from blueferry.models import Thread
from blueferry.quickshell_bridge import QuickshellBridge, RequestError, _RequestWorkers


def test_only_the_desktop_bridge_can_change_the_preferred_client(monkeypatch):
    active = []
    monkeypatch.setattr("blueferry.quickshell_bridge.record_client_use", active.append)
    widget = QuickshellBridge(FakeClient())
    with pytest.raises(RequestError, match="unsupported method"):
        widget.dispatch("client_active", {})
    assert active == []
    desktop = QuickshellBridge(FakeClient(), desktop_client=True)
    desktop.dispatch("client_active", {})
    assert active == ["quickshell"]


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def status(self):
        return SimpleNamespace(to_dict=lambda: {"daemon": True})

    def contact_photo(self, address):
        self.calls.append(("contact_photo", address))
        return self.photo

    def find_contacts(self, query):
        self.calls.append(("contacts", query))
        return [("Alice", "+15551234567")]

    def send(self, recipient, body):
        self.calls.append(("send", recipient, body))
        return "message-handle"

    def send_to_thread(self, thread_key, body, *, confirm_group, expected_group_token):
        self.calls.append(("send_to_thread", thread_key, body, confirm_group, expected_group_token))
        return "thread-message-handle"

    def set_group_participants(self, thread_key, recipients):
        self.calls.append(("set_group_participants", thread_key, recipients))
        return SimpleNamespace(to_dict=lambda: {"key": thread_key})

    def delete_threads(self, thread_keys):
        self.calls.append(("delete_threads", thread_keys))
        return len(thread_keys)

    def mark_thread_read(self, thread_key):
        self.calls.append(("mark_thread_read", thread_key))
        return 2

    def set_thread_starred(self, thread_key, starred):
        self.calls.append(("set_thread_starred", thread_key, starred))
        return starred

    def set_ancs_notification_actions(self, enabled):
        self.calls.append(("set_ancs_notification_actions", enabled))
        return enabled

    def set_contacts_only_notifications(self, enabled):
        self.calls.append(("set_contacts_only_notifications", enabled))
        return enabled

    def set_media_control(self, enabled):
        self.calls.append(("set_media_control", enabled))
        return {"media_control_enabled": enabled}

    def set_mpris_player(self, enabled):
        self.calls.append(("set_mpris_player", enabled))
        return {"media_mpris_enabled": enabled}

    def set_auto_profile_reset(self, enabled):
        self.calls.append(("set_auto_profile_reset", enabled))
        return enabled

    def set_proximity_lock(self, enabled, grace_seconds):
        self.calls.append(("set_proximity_lock", enabled, grace_seconds))
        return {
            "proximity_lock_enabled": enabled,
            "proximity_lock_grace_sec": grace_seconds,
        }

    def set_call_history(self, enabled, missed_call_notifications):
        self.calls.append(("set_call_history", enabled, missed_call_notifications))
        return {
            "call_history_enabled": enabled,
            "missed_call_notifications": missed_call_notifications,
        }

    def set_calls_enabled(self, enabled):
        self.calls.append(("set_calls_enabled", enabled))
        return {"calls_enabled": enabled}

    def set_phone_audio_route(self, route):
        self.calls.append(("set_phone_audio_route", route))
        return route


def test_bridge_dispatches_private_values_without_command_arguments() -> None:
    client = FakeClient()
    bridge = QuickshellBridge(client)  # type: ignore[arg-type]

    assert bridge.dispatch("contacts", {"query": "private search"}) == [
        {"name": "Alice", "address": "+15551234567"}
    ]
    assert bridge.dispatch("send", {
        "recipient": "+15557654321", "body": "private body"
    }) == "message-handle"
    assert bridge.dispatch("send_to_thread", {
        "thread_key": "private-thread",
        "body": "group secret",
        "confirm_group": True,
        "expected_group_token": "roster-token",
    }) == "thread-message-handle"
    bridge.dispatch("set_group_participants", {
        "thread_key": "private-thread",
        "recipients": ["+15550000001", "+15550000002"],
    })
    assert bridge.dispatch("delete_threads", {
        "thread_keys": ["thread-one", "thread-two"],
    }) == 2
    assert bridge.dispatch("mark_thread_read", {
        "thread_key": "private-thread",
    }) == 2
    assert bridge.dispatch("set_thread_starred", {
        "thread_key": "private-thread",
        "starred": True,
    }) is True
    assert bridge.dispatch("set_contacts_only_notifications", {
        "enabled": True,
    }) is True

    assert client.calls == [
        ("contacts", "private search"),
        ("send", "+15557654321", "private body"),
        ("send_to_thread", "private-thread", "group secret", True, "roster-token"),
        (
            "set_group_participants",
            "private-thread",
            ["+15550000001", "+15550000002"],
        ),
        ("delete_threads", ["thread-one", "thread-two"]),
        ("mark_thread_read", "private-thread"),
        ("set_thread_starred", "private-thread", True),
        ("set_contacts_only_notifications", True),
    ]


def test_bridge_returns_a_validated_photo_without_a_file_or_command_argument():
    from .photo_fixtures import png

    client = FakeClient()
    client.photo = png()
    bridge = QuickshellBridge(client)
    result = bridge.dispatch("contact_photo", {"address": "alice@example.com"})
    assert result == {
        "address": "alice@example.com",
        "source": "data:image/png;base64," + base64.b64encode(client.photo).decode("ascii"),
    }
    assert client.calls == [("contact_photo", "alice@example.com")]


@pytest.mark.parametrize("data", [b"", b"GIF89a", b"not a photo"])
def test_bridge_uses_the_icon_fallback_for_missing_or_invalid_photos(data):
    client = FakeClient()
    client.photo = data
    assert QuickshellBridge(client).dispatch("contact_photo", {"address": "alice@example.com"}) == {
        "address": "alice@example.com", "source": "",
    }


def test_bridge_rejects_a_photo_with_an_oversized_canvas():
    from .photo_fixtures import png_header

    client = FakeClient()
    client.photo = png_header(30000, 30000)
    assert QuickshellBridge(client).dispatch("contact_photo", {"address": "alice@example.com"})["source"] == ""


def test_bridge_sets_the_away_lock_and_returns_its_status() -> None:
    client = FakeClient()
    bridge = QuickshellBridge(client)  # type: ignore[arg-type]

    assert bridge.dispatch("set_proximity_lock", {
        "enabled": True, "grace_seconds": 90,
    }) == {"proximity_lock_enabled": True, "proximity_lock_grace_sec": 90}
    assert client.calls == [("set_proximity_lock", True, 90)]


@pytest.mark.parametrize(("args", "message"), [
    ({"enabled": 1, "grace_seconds": 60}, "enabled must be a boolean"),
    ({"enabled": True}, "grace_seconds must be an integer"),
    ({"enabled": True, "grace_seconds": True}, "grace_seconds must be an integer"),
    ({"enabled": True, "grace_seconds": "60"}, "grace_seconds must be an integer"),
])
def test_bridge_rejects_malformed_away_lock_requests(args, message) -> None:
    client = FakeClient()
    bridge = QuickshellBridge(client)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match=message):
        bridge.dispatch("set_proximity_lock", args)
    assert client.calls == []


def test_bridge_sets_the_phone_calls_opt_in_and_returns_its_status() -> None:
    client = FakeClient()
    bridge = QuickshellBridge(client)  # type: ignore[arg-type]

    assert bridge.dispatch("set_calls_enabled", {"enabled": True}) == {
        "calls_enabled": True,
    }
    assert client.calls == [("set_calls_enabled", True)]


@pytest.mark.parametrize("args", [{}, {"enabled": 1}, {"enabled": "true"}])
def test_bridge_rejects_malformed_phone_calls_requests(args) -> None:
    client = FakeClient()
    bridge = QuickshellBridge(client)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="enabled must be a boolean"):
        bridge.dispatch("set_calls_enabled", args)
    assert client.calls == []


def test_bridge_rejects_non_boolean_contacts_only_value() -> None:
    bridge = QuickshellBridge(FakeClient())  # type: ignore[arg-type]

    try:
        bridge.dispatch("set_contacts_only_notifications", {"enabled": 1})
    except ValueError as error:
        assert str(error) == "enabled must be a boolean"
    else:
        raise AssertionError("non-boolean preference was accepted")


def test_bridge_supplies_shared_thread_metadata_and_forwards_displayed_approval():
    class Client(FakeClient):
        def threads(self, limit=200):
            return [Thread.from_dict({
                "key": "group:crew", "is_group": True, "roster_warning_id": "warning:1",
                "recipients": ["Bob@example.com", "+1 (555) 222-2222"],
                "messages": [{"outgoing": False, "read": False}],
            })]

    client = Client()
    bridge = QuickshellBridge(client)
    payload = bridge.dispatch("threads", {})[0]
    assert payload["confirmation_token"] == "warning:1\n+1 (555) 222-2222\nBob@example.com"
    assert payload["roster_warning_key"] == "warning:1"
    assert payload["unread"] is True
    assert payload["unread_count"] == 1
    bridge.dispatch("send_to_thread", {
        "thread_key": payload["key"], "body": "draft", "confirm_group": True,
        "expected_group_token": payload["confirmation_token"],
    })
    assert client.calls == [(
        "send_to_thread", "group:crew", "draft", True, payload["confirmation_token"],
    )]


def test_bridge_returns_structured_success_and_errors() -> None:
    output = io.StringIO()
    bridge = QuickshellBridge(FakeClient(), output)  # type: ignore[arg-type]

    bridge.handle_line('{"id":7,"method":"status","args":{}}')
    bridge.handle_line('{"id":8,"method":"unknown","args":{}}')

    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert replies[0] == {
        "id": 7,
        "method": "status",
        "ok": True,
        "result": {
            "daemon": True,
            "bluetooth_restart_command": "sudo systemctl restart bluetooth.service",
        },
    }
    assert replies[1]["id"] == 8
    assert replies[1]["method"] == "unknown"
    assert replies[1]["ok"] is False
    assert replies[1]["error"] == "unsupported method: unknown"


def test_slow_send_does_not_block_status_requests() -> None:
    send_started = threading.Event()
    release_send = threading.Event()
    status_called = threading.Event()

    class BlockingClient(FakeClient):
        def send(self, recipient, body):
            send_started.set()
            assert release_send.wait(2)
            return "message-handle"

        def status(self):
            status_called.set()
            return super().status()

    bridge = QuickshellBridge(BlockingClient(), io.StringIO())  # type: ignore[arg-type]
    workers = _RequestWorkers(bridge)
    workers.submit('{"id":1,"method":"send","args":{"recipient":"a","body":"b"}}')
    assert send_started.wait(1)

    workers.submit('{"id":2,"method":"status","args":{}}')
    assert status_called.wait(1)
    release_send.set()


def test_focus_bypasses_busy_backend_workers_and_preserves_newer_client(monkeypatch, tmp_path):
    from blueferry import client_activation
    from blueferry.quickshell_bridge import REQUEST_WORKERS

    monkeypatch.setattr(client_activation.config, "CONFIG_DIR", tmp_path)
    busy = threading.Barrier(REQUEST_WORKERS + 1)
    release = threading.Event()

    class BlockingClient(FakeClient):
        def status(self):
            busy.wait(timeout=5)
            assert release.wait(5)
            return super().status()

    output = io.StringIO()
    bridge = QuickshellBridge(BlockingClient(), output, desktop_client=True)
    workers = _RequestWorkers(bridge)
    try:
        for index in range(REQUEST_WORKERS):
            workers.submit(json.dumps({"id": index, "method": "status", "args": {}}))
        busy.wait(timeout=5)
        workers.submit('{"id":100,"method":"client_active","args":{}}')
        assert json.loads(output.getvalue()) == {
            "id": 100, "method": "client_active", "ok": True, "result": None,
        }
        client_activation.record_client_use("gtk")
    finally:
        release.set()
    deadline = time.monotonic() + 5
    while len(output.getvalue().splitlines()) < REQUEST_WORKERS + 1:
        assert time.monotonic() < deadline
        time.sleep(.01)
    running = [client.bus_name for client in client_activation.CLIENTS]
    assert client_activation.select_client(running).key == "gtk"


@pytest.mark.parametrize("line,desktop", [
    ('{"id":true,"method":"client_active","args":{}}', True),
    ('{"id":1,"method":"client_active","args":[]}', True),
    ('{"id":1,"method":"client_active","args":{}}', False),
])
def test_immediate_focus_still_validates_requests(monkeypatch, line, desktop):
    recorded = []
    monkeypatch.setattr("blueferry.quickshell_bridge.record_client_use", recorded.append)
    output = io.StringIO()
    bridge = QuickshellBridge(FakeClient(), output, desktop_client=desktop)
    workers = _RequestWorkers(bridge)
    workers.submit(line)
    assert not json.loads(output.getvalue())["ok"]
    assert not recorded


def test_stdin_reader_drains_batched_lines_without_another_write():
    import os

    from blueferry.quickshell_bridge import _read_requests

    read_fd, write_fd = os.pipe()
    received = []
    completed = threading.Event()

    def submit(line):
        received.append(line)
        if len(received) == 3:
            completed.set()

    with os.fdopen(read_fd) as stream:
        reader = threading.Thread(target=_read_requests, args=(stream, submit), daemon=True)
        reader.start()
        try:
            os.write(write_fd, b"one\ntwo\nthree\n")
            assert completed.wait(2), received
            assert received == ["one\n", "two\n", "three\n"]
        finally:
            os.close(write_fd)
            reader.join(2)
        assert not reader.is_alive()


def test_stdin_reader_discards_oversized_line_and_recovers(monkeypatch):
    from blueferry import quickshell_bridge

    monkeypatch.setattr(quickshell_bridge, "MAX_REQUEST_CHARS", 8)
    received = []
    quickshell_bridge._read_requests(io.StringIO("x" * 40 + "\nnext\n"), received.append)
    assert len(received) == 2
    assert len(received[0]) > 8
    assert received[1] == "next\n"


def test_bridge_saves_the_notification_actions_preference() -> None:
    client = FakeClient()
    bridge = QuickshellBridge(client)  # type: ignore[arg-type]

    assert bridge.dispatch(
        "set_ancs_notification_actions", {"enabled": True}
    ) is True
    assert client.calls == [("set_ancs_notification_actions", True)]


@pytest.mark.parametrize("args", [{}, {"enabled": 1}, {"enabled": "true"}])
def test_bridge_rejects_malformed_notification_actions_requests(args) -> None:
    client = FakeClient()
    bridge = QuickshellBridge(client)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="enabled must be a boolean"):
        bridge.dispatch("set_ancs_notification_actions", args)
    assert client.calls == []


def test_status_carries_the_init_systems_bluetooth_restart_command(monkeypatch) -> None:
    from blueferry import service_manager

    monkeypatch.setattr(service_manager, "init_system", lambda: service_manager.OPENRC)
    bridge = QuickshellBridge(FakeClient())  # type: ignore[arg-type]
    assert bridge.dispatch("status", {}) == {
        "daemon": True,
        "bluetooth_restart_command": "sudo rc-service bluetooth restart",
    }
    monkeypatch.setattr(
        service_manager, "init_system", lambda: service_manager.NO_SERVICE_MANAGER,
    )
    assert QuickshellBridge(FakeClient()).dispatch(  # type: ignore[arg-type]
        "status", {},
    )["bluetooth_restart_command"] == ""


def test_host_info_is_computed_once_and_needs_no_daemon(monkeypatch) -> None:
    from blueferry import service_manager

    class NoDaemon(FakeClient):
        def status(self):
            raise AssertionError("host info must not query the daemon")

    monkeypatch.setattr(service_manager, "init_system", lambda: service_manager.OPENRC)
    output = io.StringIO()
    bridge = QuickshellBridge(NoDaemon(), output)  # type: ignore[arg-type]
    monkeypatch.setattr(
        service_manager,
        "init_system",
        lambda: (_ for _ in ()).throw(AssertionError("detected the init system again")),
    )

    # main() sends this event before serving requests.
    bridge.emit_event("host", bridge.host_info())

    assert json.loads(output.getvalue()) == {
        "event": "host",
        "data": {"bluetooth_restart_command": "sudo rc-service bluetooth restart"},
    }


@pytest.mark.parametrize("method,key", [
    ("set_media_control", "media_control_enabled"),
    ("set_mpris_player", "media_mpris_enabled"),
])
def test_bridge_saves_the_media_opt_ins_and_returns_their_status(method, key) -> None:
    client = FakeClient()
    bridge = QuickshellBridge(client)  # type: ignore[arg-type]

    assert bridge.dispatch(method, {"enabled": True}) == {key: True}
    assert client.calls == [(method, True)]
    for args in ({}, {"enabled": 1}, {"enabled": "true"}):
        with pytest.raises(ValueError, match="enabled must be a boolean"):
            bridge.dispatch(method, args)
    assert client.calls == [(method, True)]


def test_bridge_saves_the_call_history_opt_in_and_returns_its_status() -> None:
    client = FakeClient()
    bridge = QuickshellBridge(client)  # type: ignore[arg-type]

    assert bridge.dispatch(
        "set_call_history", {"enabled": True, "missed_call_notifications": False}
    ) == {"call_history_enabled": True, "missed_call_notifications": False}
    assert client.calls == [("set_call_history", True, False)]
    for args, message in (
        ({"missed_call_notifications": True}, "enabled must be a boolean"),
        ({"enabled": True}, "missed_call_notifications must be a boolean"),
        ({"enabled": True, "missed_call_notifications": 1},
         "missed_call_notifications must be a boolean"),
    ):
        with pytest.raises(ValueError, match=message):
            bridge.dispatch("set_call_history", args)
    assert len(client.calls) == 1


def test_bridge_switches_the_phone_audio_route_and_rejects_other_values() -> None:
    client = FakeClient()
    bridge = QuickshellBridge(client)  # type: ignore[arg-type]

    assert bridge.dispatch("set_phone_audio_route", {"route": "pc"}) == "pc"
    for args in ({}, {"route": "speaker"}, {"route": 1}):
        with pytest.raises(ValueError):
            bridge.dispatch("set_phone_audio_route", args)
    assert client.calls == [("set_phone_audio_route", "pc")]


def test_status_carries_the_shared_sound_switch_wording() -> None:
    from blueferry.models import BackendStatus

    client = FakeClient()
    client.status = lambda **_kwargs: BackendStatus.from_dict({  # type: ignore[method-assign]
        "daemon": True, "phone_audio_route": "pc", "phone_audio_playing": "phone",
    })
    view = QuickshellBridge(client).dispatch("status", {})["phone_audio_view"]  # type: ignore[arg-type]
    assert view["available"] is True and view["mismatch"] is True
    assert "own speaker" in view["hint"]
    # Older daemons: no key, no view.
    assert "phone_audio_view" not in QuickshellBridge(FakeClient()).dispatch(  # type: ignore[arg-type]
        "status", {},
    )


def test_bridge_saves_the_auto_profile_reset_and_returns_its_status() -> None:
    client = FakeClient()
    bridge = QuickshellBridge(client)  # type: ignore[arg-type]

    assert bridge.dispatch("set_auto_profile_reset", {"enabled": False}) == {
        "profile_reset_auto": False,
    }
    assert client.calls == [("set_auto_profile_reset", False)]
    with pytest.raises(ValueError, match="enabled must be a boolean"):
        bridge.dispatch("set_auto_profile_reset", {"enabled": "no"})
    assert len(client.calls) == 1


def test_bridge_reduces_now_playing_to_booleans_and_verbatim_text() -> None:
    snapshot = {
        "enabled": True, "available": True,
        "player": {"state": "playing"},
        "track": {"title": "<b>Song</b>", "artist": "Band"},
        "supported_commands": ["pause", "next"],
    }
    bridge = QuickshellBridge(SimpleNamespace(now_playing=lambda: snapshot))  # type: ignore[arg-type]

    result = bridge.dispatch("now_playing", {})
    assert result == {
        "visible": True, "playing": True, "title": "<b>Song</b>", "artist": "Band",
        "idle": False, "previous": False, "toggle": True, "next": True,
    }
    # No arrays reach QML, whatever the Qt version does with them.
    assert not any(isinstance(value, list | tuple) for value in result.values())
    assert json.loads(json.dumps(result)) == result


@pytest.mark.parametrize("snapshot", [
    {"enabled": False, "available": False, "detail": "disabled"},
    {"enabled": True, "available": False, "detail": "waiting-for-iphone"},
])
def test_bridge_hides_now_playing_until_the_media_service_is_available(snapshot) -> None:
    bridge = QuickshellBridge(SimpleNamespace(now_playing=lambda: snapshot))  # type: ignore[arg-type]

    assert bridge.dispatch("now_playing", {}) == {
        "visible": False, "playing": False, "title": "", "artist": "", "idle": False,
        "previous": False, "toggle": False, "next": False,
    }


@pytest.mark.parametrize("state", [None, "paused", "stopped"])
@pytest.mark.parametrize("track", [
    {},
    {"title": None, "artist": None, "album": None, "duration": None},
    {"title": " \t", "artist": "\n", "album": "An album"},
])
def test_bridge_hides_empty_inactive_media_after_reconnect(state, track) -> None:
    snapshot = {
        "enabled": True, "available": True,
        "player": {"name": "Music", "state": state, "volume": 0.0},
        "track": track,
        "supported_commands": ["volume-up", "volume-down"],
        "supported_commands_known": True,
    }
    bridge = QuickshellBridge(SimpleNamespace(now_playing=lambda: snapshot))  # type: ignore[arg-type]

    assert bridge.dispatch("now_playing", {}) == {
        "visible": False, "playing": False, "title": "", "artist": "", "idle": False,
        "previous": False, "toggle": False, "next": False,
    }


@pytest.mark.parametrize("state", ["playing", "rewinding", "fast-forwarding"])
def test_bridge_keeps_active_playback_visible_before_track_metadata(state) -> None:
    snapshot = {
        "enabled": True, "available": True, "player": {"state": state}, "track": {},
        "supported_commands": ["pause"],
    }
    bridge = QuickshellBridge(SimpleNamespace(now_playing=lambda: snapshot))  # type: ignore[arg-type]

    view = bridge.dispatch("now_playing", {})
    assert view["visible"] and view["playing"] and view["toggle"]
    assert not view["idle"]


@pytest.mark.parametrize("track", [{"title": "A podcast"}, {"artist": "A broadcaster"}])
@pytest.mark.parametrize("state", [None, "paused"])
def test_bridge_keeps_named_media_visible_without_active_playback(track, state) -> None:
    snapshot = {
        "enabled": True, "available": True, "player": {"state": state}, "track": track,
        "supported_commands": ["play"],
    }
    bridge = QuickshellBridge(SimpleNamespace(now_playing=lambda: snapshot))  # type: ignore[arg-type]

    view = bridge.dispatch("now_playing", {})
    assert view["visible"] and view["toggle"]
    assert not view["playing"] and not view["idle"]
    assert view["title"] == track.get("title", "")
    assert view["artist"] == track.get("artist", "")


def test_bridge_sends_only_the_transport_media_commands() -> None:
    sent: list[str] = []
    bridge = QuickshellBridge(SimpleNamespace(send_media_command=sent.append))  # type: ignore[arg-type]

    assert bridge.dispatch("media_command", {"command": "next"}) is None
    for args, message in (
        ({}, "command must be a string"),
        ({"command": 1}, "command must be a string"),
        ({"command": "volume-up"}, "unsupported media command"),
        ({"command": "like"}, "unsupported media command"),
    ):
        with pytest.raises(ValueError, match=message):
            bridge.dispatch("media_command", args)
    assert sent == ["next"]


def test_bridge_saves_the_contact_photo_opt_in() -> None:
    client = FakeClient()
    client.set_contact_photos = lambda enabled: (  # type: ignore[attr-defined]
        client.calls.append(("set_contact_photos", enabled)) or {"contact_photos": enabled}
    )
    bridge = QuickshellBridge(client)  # type: ignore[arg-type]

    assert bridge.dispatch("set_contact_photos", {"enabled": True}) == {"contact_photos": True}
    assert client.calls == [("set_contact_photos", True)]
    try:
        bridge.dispatch("set_contact_photos", {"enabled": "yes"})
    except ValueError as error:
        assert str(error) == "enabled must be a boolean"
    else:
        raise AssertionError("non-boolean preference was accepted")


def test_bridge_saves_the_one_time_code_opt_in() -> None:
    client = FakeClient()
    client.set_otp_autocopy = lambda enabled: (  # type: ignore[attr-defined]
        client.calls.append(("set_otp_autocopy", enabled)) or {"otp_autocopy": enabled}
    )
    bridge = QuickshellBridge(client)  # type: ignore[arg-type]

    assert bridge.dispatch("set_otp_autocopy", {"enabled": True}) == {"otp_autocopy": True}
    assert client.calls == [("set_otp_autocopy", True)]
    try:
        bridge.dispatch("set_otp_autocopy", {"enabled": "yes"})
    except ValueError as error:
        assert str(error) == "enabled must be a boolean"
    else:
        raise AssertionError("non-boolean preference was accepted")


# ---- QS-1: settings parity (features, reconnect, sync, notifications) -------


def _no_arrays(value: object) -> bool:
    """Nothing the bridge hands to QML may be an array (#213)."""
    if isinstance(value, list | tuple):
        return False
    if isinstance(value, dict):
        return all(_no_arrays(item) for item in value.values())
    return True


class SettingsClient(FakeClient):
    def __init__(self, status: dict | None = None) -> None:
        super().__init__()
        self.status_value = {"daemon": True, **(status or {})}
        self.feature_values: object = {
            "show_notification_content": {
                "value": True, "running": True, "source": "default",
                "variable": "BLUEFERRY_SHOW_NOTIFICATION_CONTENT", "restart_required": False,
            },
            "mark_read_on_dismiss": {
                "value": True, "running": False, "source": "settings",
                "variable": "BLUEFERRY_MARK_READ_ON_DISMISS", "restart_required": True,
            },
            "notification_history": {
                "value": False, "running": False, "source": "environment",
                "variable": "BLUEFERRY_NOTIFICATION_HISTORY", "restart_required": False,
            },
        }
        self.notification_snapshot: object = {"enabled": True, "content": True, "notifications": []}
        self.reconnect_result = "started"

    def status(self):
        return SimpleNamespace(to_dict=lambda: dict(self.status_value))

    def features(self):
        self.calls.append(("features",))
        if isinstance(self.feature_values, Exception):
            raise self.feature_values
        return self.feature_values

    def set_feature(self, name, enabled):
        self.calls.append(("set_feature", name, enabled))
        return "restart-required"

    def set_phone_battery_warning(self, enabled):
        self.calls.append(("set_phone_battery_warning", enabled))
        return enabled

    def set_mirror_notification_removals(self, enabled):
        self.calls.append(("set_mirror_notification_removals", enabled))
        return enabled

    def reconnect_phone(self):
        self.calls.append(("reconnect_phone",))
        return self.reconnect_result

    def sync_contacts(self):
        self.calls.append(("sync_contacts",))
        return 42

    def notifications(self, limit=50):
        self.calls.append(("notifications", limit))
        if isinstance(self.notification_snapshot, Exception):
            raise self.notification_snapshot
        return self.notification_snapshot


def test_bridge_flattens_the_feature_switches_without_arrays() -> None:
    reply = QuickshellBridge(SettingsClient()).dispatch("features", {})  # type: ignore[arg-type]
    assert _no_arrays(reply)
    assert reply["available"] is True and reply["count"] == 3
    rows = {reply[f"item{index}"]["name"]: reply[f"item{index}"] for index in range(3)}
    assert rows["show_notification_content"] == {
        "name": "show_notification_content",
        "variable": "BLUEFERRY_SHOW_NOTIFICATION_CONTENT",
        "usable": True, "active": True, "editable": True, "note": "",
    }
    assert rows["mark_read_on_dismiss"]["note"] == "Applies after the service restarts."
    assert rows["notification_history"]["editable"] is False
    assert "BLUEFERRY_NOTIFICATION_HISTORY" in rows["notification_history"]["note"]
    assert reply["restart_pending"] is True
    assert reply["restart_notice"] == "Some changes apply after the BlueFerry service restarts."


def test_bridge_names_the_local_env_variable_for_an_older_daemon() -> None:
    from blueferry.client import BackendError

    client = SettingsClient()
    client.feature_values = BackendError("No such method 'GetFeatures'")
    reply = QuickshellBridge(client).dispatch("features", {})  # type: ignore[arg-type]
    assert _no_arrays(reply)
    assert reply["available"] is False and reply["error"] == "No such method 'GetFeatures'"
    assert reply["count"] == 3
    for index in range(3):
        row = reply[f"item{index}"]
        assert row["usable"] is False and row["editable"] is False
        assert row["variable"] in row["note"] and "local.env" in row["note"]
    assert reply["restart_pending"] is False


def test_bridge_saves_a_feature_and_returns_the_notice_with_the_reloaded_view() -> None:
    client = SettingsClient()
    reply = QuickshellBridge(client).dispatch(  # type: ignore[arg-type]
        "set_feature", {"name": "mark_read_on_dismiss", "enabled": True},
    )
    assert client.calls == [("set_feature", "mark_read_on_dismiss", True), ("features",)]
    assert _no_arrays(reply)
    assert reply["result"] == "restart-required"
    assert reply["notice"] == "Restart the BlueFerry service to apply the change."
    assert reply["restart_notice"] == reply["notice"]


@pytest.mark.parametrize(("args", "message"), [
    ({"enabled": True}, "name must be a string"),
    ({"name": 3, "enabled": True}, "name must be a string"),
    ({"name": "Mark Read", "enabled": True}, "name must be a feature name"),
    ({"name": "x" * 65, "enabled": True}, "name must be a feature name"),
    ({"name": "mark_read_on_dismiss", "enabled": "true"}, "enabled must be a boolean"),
    ({"name": "mark_read_on_dismiss"}, "enabled must be a boolean"),
])
def test_bridge_rejects_malformed_feature_requests(args, message) -> None:
    client = SettingsClient()
    with pytest.raises(RequestError, match=message):
        QuickshellBridge(client).dispatch("set_feature", args)  # type: ignore[arg-type]
    assert client.calls == []


def test_bridge_restarts_the_service(monkeypatch) -> None:
    restarted = []
    monkeypatch.setattr(
        "blueferry.quickshell_bridge.restart_backend", lambda: restarted.append(True),
    )
    assert QuickshellBridge(SettingsClient()).dispatch(  # type: ignore[arg-type]
        "restart_backend", {},
    ) is None
    assert restarted == [True]


@pytest.mark.parametrize(("method", "status_key"), [
    ("set_phone_battery_warning", "phone_battery_warning"),
    ("set_mirror_notification_removals", "mirror_iphone_removals"),
])
def test_bridge_saves_battery_warning_and_notification_sync(method, status_key) -> None:
    client = SettingsClient()
    bridge = QuickshellBridge(client)  # type: ignore[arg-type]
    assert bridge.dispatch(method, {"enabled": True}) is True
    assert client.calls == [(method, True)]
    for bad in ({}, {"enabled": 1}, {"enabled": "false"}):
        with pytest.raises(RequestError, match="enabled must be a boolean"):
            bridge.dispatch(method, bad)
    assert client.calls == [(method, True)]


def test_bridge_reconnects_with_the_shared_texts_until_the_status_shows_the_outcome() -> None:
    from blueferry import reconnect_view

    client = SettingsClient({"phone_reconnect_state": "waiting"})
    bridge = QuickshellBridge(client)  # type: ignore[arg-type]
    # Older daemons without the reconnect keys get no row at all.
    assert "reconnect_view" not in QuickshellBridge(SettingsClient()).dispatch(  # type: ignore[arg-type]
        "status", {},
    )
    assert bridge.dispatch("status", {})["reconnect_view"] == {
        "available": True, "offered": True, "attention": False,
        "hint": "Reconnecting automatically.", "error": "",
    }
    client.reconnect_result = "started"
    assert bridge.dispatch("reconnect_phone", {}) == {
        "result": "started", "error": "", "notice": reconnect_view.STARTED_TEXT,
    }
    client.status_value["phone_reconnect_state"] = "connecting"
    assert bridge.dispatch("status", {})["reconnect_view"]["hint"] == reconnect_view.STARTED_TEXT
    # The status shows the outcome once: an unreachable phone is an error.
    client.status_value["phone_reconnect_state"] = "unreachable"
    view = bridge.dispatch("status", {})["reconnect_view"]
    assert view["error"] == reconnect_view.UNREACHABLE_TEXT
    assert bridge.dispatch("status", {})["reconnect_view"]["error"] == ""
    client.reconnect_result = "unreachable"
    assert bridge.dispatch("reconnect_phone", {})["error"] == reconnect_view.UNREACHABLE_TEXT


def test_bridge_hides_the_reconnect_button_while_bluetoothd_hangs() -> None:
    client = SettingsClient({"phone_reconnect_state": "waiting", "bluez_unresponsive": True})
    view = QuickshellBridge(client).dispatch("status", {})["reconnect_view"]  # type: ignore[arg-type]
    assert view["available"] is True and view["offered"] is False
    assert view["attention"] is True


def test_bridge_syncs_contacts_and_words_the_count() -> None:
    client = SettingsClient()
    assert QuickshellBridge(client).dispatch("sync_contacts", {}) == {  # type: ignore[arg-type]
        "count": 42, "text": "Synced 42 contact destinations",
    }


def test_bridge_does_not_read_notifications_while_the_list_is_off() -> None:
    client = SettingsClient({"notification_history_enabled": False})
    reply = QuickshellBridge(client).dispatch("notifications", {})  # type: ignore[arg-type]
    assert ("notifications", 30) not in client.calls
    assert _no_arrays(reply)
    assert reply["off"] is True and reply["count"] == 0
    assert reply["title"] == "Notification List Is Off"
    assert "Keep recent iPhone notifications" in reply["explanation"]


def test_bridge_flattens_the_newest_notifications_and_caps_them() -> None:
    client = SettingsClient({"notification_history_enabled": True})
    client.notification_snapshot = {
        "enabled": True, "content": False,
        "notifications": [
            {"app_name": "<b>Mail</b>", "title": f"T{index}", "subtitle": "S",
             "body": "line\nnext", "time": ""}
            for index in range(40)
        ],
    }
    reply = QuickshellBridge(client).dispatch("notifications", {})  # type: ignore[arg-type]
    assert client.calls[-1] == ("notifications", 30)
    assert _no_arrays(reply)
    assert reply["count"] == 30 and "item30" not in reply
    # Verbatim, never markup: the shell shows it as plain text.
    assert reply["item0"] == {"app": "<b>Mail</b>", "time": "", "summary": "T0 · S",
                              "body": "line\nnext"}
    assert reply["off"] is False and reply["failed"] is False
    assert "Only app names and times" in reply["content_hint"]


def test_bridge_explains_an_empty_or_failing_notification_list() -> None:
    from blueferry.client import BackendError

    client = SettingsClient({"notification_history_enabled": True})
    empty = QuickshellBridge(client).dispatch("notifications", {})  # type: ignore[arg-type]
    assert empty["title"] == "No Notifications Yet" and empty["count"] == 0
    assert empty["content_hint"] == ""
    client.notification_snapshot = BackendError("ListNotifications failed")
    failing = QuickshellBridge(client).dispatch("notifications", {})  # type: ignore[arg-type]
    assert failing["failed"] is True and failing["count"] == 0
    assert failing["title"] == "Notifications Are Unavailable"
    assert failing["explanation"] == "ListNotifications failed"


def test_bridge_runs_diagnostics_with_the_shared_summary(monkeypatch) -> None:
    from blueferry import doctor_report

    monkeypatch.setattr(
        doctor_report, "run_doctor",
        lambda: doctor_report.DoctorReport(True, True, "WARNING: ancs"),
    )
    reply = QuickshellBridge(SettingsClient()).dispatch("doctor", {})  # type: ignore[arg-type]
    assert reply == {
        "running": False, "ran": True, "ok": True, "warnings": True,
        "text": "WARNING: ancs", "level": "warning",
        "summary": "Checks completed with warnings.",
    }


def test_bridge_forwards_the_content_free_notifications_signal(monkeypatch) -> None:
    from blueferry import quickshell_bridge

    receivers = {}

    class Bus:
        def add_signal_receiver(self, handler, signal_name, **_kwargs):
            receivers[signal_name] = handler
            return SimpleNamespace(remove=lambda: None)

    monkeypatch.setattr(quickshell_bridge, "get_session_bus", lambda: Bus())
    output = io.StringIO()
    bridge = QuickshellBridge(FakeClient(), output)  # type: ignore[arg-type]
    quickshell_bridge._install_signal_receivers(bridge)
    receivers["NotificationsChanged"]()
    assert json.loads(output.getvalue()) == {"event": "notifications-changed", "data": None}
