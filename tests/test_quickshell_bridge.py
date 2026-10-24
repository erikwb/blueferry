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


class TetherClient:
    def __init__(self, *, unsupported: bool = False) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.unsupported = unsupported
        self.enabled = False
        self.autoconnect = False

    def _status(self, name, state="off", *args):
        from blueferry.client import TetherUnsupportedError
        from blueferry.tether_status import TetherStatus

        self.calls.append((name, *args))
        if self.unsupported:
            raise TetherUnsupportedError("no Tether1")
        return TetherStatus.from_dict({
            "state": state, "enabled": self.enabled, "autoconnect": self.autoconnect,
        })

    def tether_state(self):
        return self._status("state")

    def tether_connect(self):
        return self._status("connect", "connecting")

    def tether_disconnect(self):
        return self._status("disconnect", "disconnecting")

    def tether_configure(self, enabled, autoconnect):
        self.enabled, self.autoconnect = enabled, autoconnect
        return self._status("configure", "off", enabled, autoconnect)


def test_bridge_dispatches_tethering_with_the_shared_summary() -> None:
    client = TetherClient()
    bridge = QuickshellBridge(client)  # type: ignore[arg-type]

    state = bridge.dispatch("tether_state", {})
    assert state["available"] is True
    assert state["enabled"] is False
    assert "turned off" in state["summary"]

    configured = bridge.dispatch("tether_configure", {"enabled": True, "autoconnect": False})
    assert (configured["enabled"], configured["autoconnect"]) == (True, False)
    assert "Not sharing" in configured["summary"]
    assert bridge.dispatch("tether_connect", {})["state"] == "connecting"
    assert bridge.dispatch("tether_disconnect", {})["state"] == "disconnecting"
    assert client.calls == [
        ("state",), ("configure", True, False), ("connect",), ("disconnect",),
    ]


@pytest.mark.parametrize("args", [
    {"enabled": "yes", "autoconnect": False},
    {"enabled": True},
    {"enabled": True, "autoconnect": 1},
])
def test_bridge_rejects_non_boolean_tether_settings(args) -> None:
    client = TetherClient()
    bridge = QuickshellBridge(client)  # type: ignore[arg-type]
    with pytest.raises(RequestError, match="must be a boolean"):
        bridge.dispatch("tether_configure", args)
    assert client.calls == []


def test_bridge_hides_tethering_for_a_daemon_without_tether1() -> None:
    bridge = QuickshellBridge(TetherClient(unsupported=True))  # type: ignore[arg-type]
    assert bridge.dispatch("tether_state", {}) == {"available": False}


def test_tether_changed_is_forwarded_as_a_content_free_event(monkeypatch) -> None:
    from blueferry import quickshell_bridge
    from blueferry.protocol import TETHER_IFACE

    receivers: dict[str, tuple] = {}

    class _Bus:
        def add_signal_receiver(self, handler, signal_name, **keywords):
            receivers[signal_name] = (handler, keywords)
            return signal_name

    monkeypatch.setattr(quickshell_bridge, "get_session_bus", lambda: _Bus())
    output = io.StringIO()
    bridge = QuickshellBridge(FakeClient(), output)  # type: ignore[arg-type]

    quickshell_bridge._install_signal_receivers(bridge)
    handler, keywords = receivers["TetherChanged"]
    assert keywords["dbus_interface"] == TETHER_IFACE
    handler()

    assert json.loads(output.getvalue()) == {"event": "tether-changed", "data": None}


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
