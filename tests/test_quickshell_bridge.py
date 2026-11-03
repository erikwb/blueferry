from __future__ import annotations

import base64
import io
import json
import threading
import time
from types import SimpleNamespace

import pytest

from blueferry import notification_pin_view
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


@pytest.mark.parametrize("handle", ["", "message-7"])
def test_activation_forwards_the_handle_but_never_the_token(handle):
    output = io.StringIO()
    bridge = QuickshellBridge(FakeClient(), output, desktop_client=True)
    bridge.activation_requested(handle, "single-use-token")
    assert json.loads(output.getvalue()) == {"event": "open-message", "data": handle}


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

    def call_audio_devices(self):
        self.calls.append(("call_audio_devices",))
        return {"outputs": [{"name": "alsa_output.a50", "description": "A50", "default": True}],
                "inputs": []}

    def set_call_audio_device(self, direction, name):
        self.calls.append(("set_call_audio_device", direction, name))
        return {f"calls_audio_{direction}": name}


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
    assert view["sync"] == ""
    client.status = lambda **_kwargs: BackendStatus.from_dict({  # type: ignore[method-assign]
        "daemon": True, "phone_audio_route": "pc",
        "phone_audio_delay_ms": 110, "phone_audio_delay_reporting": True,
    })
    view = QuickshellBridge(client).dispatch("status", {})["phone_audio_view"]  # type: ignore[arg-type]
    assert view["sync"] == "A/V sync: this computer reports 110 ms."
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
    volume = result.pop("volume")
    assert result == {
        "visible": True, "playing": True, "title": "<b>Song</b>", "artist": "Band",
        "idle": False, "previous": False, "toggle": True, "next": True,
    }
    # This player offers no volume step: shown, but disabled with the reason.
    assert volume["visible"] is True
    assert not volume["up_enabled"] and not volume["down_enabled"]
    assert "does not offer volume control" in volume["up_tooltip"]
    result["volume"] = volume
    # No arrays reach QML, whatever the Qt version does with them.
    assert not any(isinstance(value, list | tuple) for value in result.values())
    assert not any(isinstance(value, list | tuple) for value in volume.values())
    assert json.loads(json.dumps(result)) == result


HIDDEN_VOLUME = {
    "visible": False, "up_enabled": False, "down_enabled": False, "level": None,
    "level_text": "", "up_tooltip": "", "down_tooltip": "",
    "up_label": "Volume Up", "down_label": "Volume Down",
    "up_command": "volume-up", "down_command": "volume-down",
}


@pytest.mark.parametrize("snapshot", [
    {"enabled": False, "available": False, "detail": "disabled"},
    {"enabled": True, "available": False, "detail": "waiting-for-iphone"},
])
def test_bridge_hides_now_playing_until_the_media_service_is_available(snapshot) -> None:
    bridge = QuickshellBridge(SimpleNamespace(now_playing=lambda: snapshot))  # type: ignore[arg-type]

    assert bridge.dispatch("now_playing", {}) == {
        "visible": False, "playing": False, "title": "", "artist": "", "idle": False,
        "previous": False, "toggle": False, "next": False, "volume": HIDDEN_VOLUME,
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
        "previous": False, "toggle": False, "next": False, "volume": HIDDEN_VOLUME,
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


def test_bridge_shows_the_shared_volume_buttons_inside_the_strip() -> None:
    snapshot = {
        "enabled": True, "available": True,
        "player": {"state": "playing", "volume": 0.4},
        "track": {"title": "Song"},
        "supported_commands": ["pause", "volume-up", "volume-down"],
        "supported_commands_known": True,
    }
    bridge = QuickshellBridge(SimpleNamespace(now_playing=lambda: snapshot))  # type: ignore[arg-type]

    volume = bridge.dispatch("now_playing", {})["volume"]
    assert volume == {
        "visible": True, "up_enabled": True, "down_enabled": True, "level": 40,
        "level_text": "Volume 40%",
        "up_tooltip": "Turn the iPhone volume up",
        "down_tooltip": "Turn the iPhone volume down",
        "up_label": "Volume Up", "down_label": "Volume Down",
        "up_command": "volume-up", "down_command": "volume-down",
    }


def test_bridge_sends_the_transport_and_volume_media_commands() -> None:
    sent: list[str] = []
    bridge = QuickshellBridge(SimpleNamespace(send_media_command=sent.append))  # type: ignore[arg-type]

    assert bridge.dispatch("media_command", {"command": "next"}) is None
    assert bridge.dispatch("media_command", {"command": "volume-up"}) is None
    assert bridge.dispatch("media_command", {"command": "volume-down"}) is None
    for args, message in (
        ({}, "command must be a string"),
        ({"command": 1}, "command must be a string"),
        ({"command": "volume"}, "unsupported media command"),
        ({"command": "like"}, "unsupported media command"),
    ):
        with pytest.raises(ValueError, match=message):
            bridge.dispatch("media_command", args)
    assert sent == ["next", "volume-up", "volume-down"]


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


def test_bridge_resets_storage_only_when_the_request_is_confirmed() -> None:
    client = FakeClient()
    client.reset_storage = lambda: (  # type: ignore[attr-defined]
        client.calls.append(("reset_storage",)) or {"storage_state": "ready"}
    )
    bridge = QuickshellBridge(client)  # type: ignore[arg-type]

    for args in ({}, {"confirmed": False}, {"confirmed": "yes"}, {"confirmed": 1}):
        with pytest.raises(RequestError):
            bridge.dispatch("reset_storage", args)
    assert client.calls == []
    assert bridge.dispatch("reset_storage", {"confirmed": True}) == {"storage_state": "ready"}
    assert client.calls == [("reset_storage",)]


# ---- QS-1: settings parity (features, reconnect, sync, notifications) -------


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


def test_bridge_lists_the_feature_switches_with_the_shared_notes() -> None:
    reply = QuickshellBridge(SettingsClient()).dispatch("features", {})  # type: ignore[arg-type]
    assert reply["available"] is True and len(reply["switches"]) == 3
    assert json.loads(json.dumps(reply)) == reply
    rows = {row["name"]: row for row in reply["switches"]}
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
    assert reply["available"] is False and reply["error"] == "No such method 'GetFeatures'"
    assert len(reply["switches"]) == 3
    for row in reply["switches"]:
        assert row["usable"] is False and row["editable"] is False
        assert row["variable"] in row["note"] and "local.env" in row["note"]
    assert reply["restart_pending"] is False


def test_bridge_saves_a_feature_and_returns_the_notice_with_the_reloaded_view() -> None:
    client = SettingsClient()
    reply = QuickshellBridge(client).dispatch(  # type: ignore[arg-type]
        "set_feature", {"name": "mark_read_on_dismiss", "enabled": True},
    )
    assert client.calls == [("set_feature", "mark_read_on_dismiss", True), ("features",)]
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
    assert reply["off"] is True and reply["rows"] == []
    assert reply["title"] == "Notification List Is Off"
    assert "Keep recent iPhone notifications" in reply["explanation"]


def test_bridge_lists_the_newest_notifications_and_caps_them() -> None:
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
    assert len(reply["rows"]) == 30
    # Verbatim, never markup: the shell shows it as plain text.
    first = reply["rows"][0]
    assert {key: first[key] for key in ("app", "time", "summary", "body")} == {
        "app": "<b>Mail</b>", "time": "", "summary": "T0 · S", "body": "line\nnext",
    }
    # An older daemon without keys: no pin control, nothing dimmed.
    assert first["can_pin"] is False and first["dimmed"] is False
    assert reply["pin_limit_reached"] is False and reply["pin_limit_text"] == ""
    assert reply["off"] is False and reply["failed"] is False
    assert "Only app names and times" in reply["content_hint"]


def test_bridge_explains_an_empty_or_failing_notification_list() -> None:
    from blueferry.client import BackendError

    client = SettingsClient({"notification_history_enabled": True})
    empty = QuickshellBridge(client).dispatch("notifications", {})  # type: ignore[arg-type]
    assert empty["title"] == "No Notifications Yet" and empty["rows"] == []
    assert empty["content_hint"] == ""
    client.notification_snapshot = BackendError("ListNotifications failed")
    failing = QuickshellBridge(client).dispatch("notifications", {})  # type: ignore[arg-type]
    assert failing["failed"] is True and failing["rows"] == []
    assert failing["title"] == "Notifications Are Unavailable"
    assert failing["explanation"] == "ListNotifications failed"


def test_bridge_words_an_unreadable_status_in_the_notification_replies() -> None:
    from blueferry.client import BackendError

    client = NotificationAppsClient()

    def no_status(**_kwargs):
        raise BackendError("")

    client.status = no_status  # type: ignore[method-assign]
    bridge = QuickshellBridge(client)  # type: ignore[arg-type]
    listed = bridge.dispatch("notifications", {})
    assert listed["failed"] is True and listed["rows"] == []
    assert listed["title"] == "Notifications Are Unavailable"
    assert listed["explanation"] == "Notifications are unavailable"
    apps = bridge.dispatch("notification_apps", {})
    assert apps["rows"] == [] and apps["error"] == "Could not read the muted apps."


def test_bridge_runs_diagnostics_with_the_shared_summary(monkeypatch) -> None:
    from blueferry import doctor_report

    monkeypatch.setattr(
        doctor_report.DoctorRunner, "run",
        lambda _self: doctor_report.DoctorReport(True, True, "WARNING: ancs"),
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


@pytest.mark.real_glib_sources
@pytest.mark.parametrize("number", ["SIGTERM", "SIGHUP"])
def test_bridge_stops_diagnostics_when_quickshell_terminates_it(monkeypatch, number) -> None:
    """A signal ends the loop through main's cleanup, which stops a doctor child."""
    import os
    import signal

    from gi.repository import GLib

    from blueferry import doctor_report, quickshell_bridge

    stopped, timed_out = [], []

    class IteratingLoop:
        """GLib.MainLoop.run would arm and remove its own SIGINT source."""

        def __init__(self) -> None:
            self.running = False

        def run(self) -> None:
            self.running = True
            context = GLib.MainContext.default()
            while self.running:
                context.iteration(True)

        def quit(self) -> None:
            self.running = False

    def guard() -> bool:
        timed_out.append(True)
        os.kill(os.getpid(), signal.SIGTERM)  # end a loop the signal did not
        return False

    read_end, write_end = os.pipe()
    monkeypatch.setattr(quickshell_bridge, "BackendClient", FakeClient)
    monkeypatch.setattr(quickshell_bridge, "_install_signal_receivers", lambda _bridge: [])
    monkeypatch.setattr(doctor_report.DoctorRunner, "stop", lambda _self: stopped.append(True))
    monkeypatch.setattr(GLib, "MainLoop", IteratingLoop)
    monkeypatch.setattr("sys.argv", ["blueferry-quickshell-bridge"])
    monkeypatch.setattr("sys.stdout", io.StringIO())
    stdin = os.fdopen(read_end, "r")  # never written: the reader keeps waiting
    monkeypatch.setattr("sys.stdin", stdin)
    kill = GLib.timeout_add(50, lambda: os.kill(os.getpid(), getattr(signal, number)) or False)
    guard_source = GLib.timeout_add(5000, guard)
    try:
        assert quickshell_bridge.main() == 0
    finally:
        for source_id in (kill, guard_source):
            source = GLib.MainContext.default().find_source_by_id(source_id)
            if source is not None:
                source.destroy()
        os.close(write_end)
    assert timed_out == []
    assert stopped == [True]


def test_bridge_returns_the_call_audio_rows_and_saves_a_device() -> None:
    client = FakeClient()
    bridge = QuickshellBridge(client)  # type: ignore[arg-type]

    # Calls off or an older daemon: no rows, and PipeWire is not asked.
    assert bridge.dispatch("call_audio_settings", {})["supported"] is False
    assert client.calls == []

    client.status = lambda: SimpleNamespace(to_dict=lambda: {
        "daemon": True, "calls_audio_output": "alsa_output.a50", "calls_audio_input": "",
    })
    view = bridge.dispatch("call_audio_settings", {})
    assert view["supported"] is True
    assert view["rows"][0]["label"] == "Speaker for calls"
    assert view["rows"][0]["index"] == 1
    assert bridge.dispatch("set_call_audio_device", {"direction": "input", "name": ""}) == {
        "calls_audio_input": "",
    }
    assert client.calls == [("call_audio_devices",), ("set_call_audio_device", "input", "")]


@pytest.mark.parametrize("args", [
    {}, {"direction": "speaker", "name": "x"}, {"direction": "output"},
    {"direction": "output", "name": 5},
])
def test_bridge_rejects_malformed_call_audio_requests(args) -> None:
    client = FakeClient()
    bridge = QuickshellBridge(client)  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        bridge.dispatch("set_call_audio_device", args)
    assert client.calls == []


# ---- Welle B: pinned notifications and muted apps -----------------------------


class NotificationAppsClient(SettingsClient):
    def __init__(self, status: dict | None = None) -> None:
        super().__init__({
            "notification_history_enabled": True,
            "notification_muted_app_count": 1,
            **(status or {}),
        })
        self.apps: object = {
            "apps": [
                {"app_id": "com.example.chat", "app_name": "Chat", "muted": True,
                 "seen": True, "last_seen": 1},
                {"app_id": "com.example.mail", "app_name": "Mail", "muted": False,
                 "seen": False, "last_seen": None},
            ],
            "max_muted": 100, "mute_available": True, "all_notifications": True,
            "env_allowlist": False, "env_blocklist": False,
        }
        self.pin_error: Exception | None = None
        self.mute_error: Exception | None = None
        self.before_mute = None

    def notification_apps(self):
        self.calls.append(("notification_apps",))
        if isinstance(self.apps, Exception):
            raise self.apps
        return json.loads(json.dumps(self.apps))

    def set_notification_app_muted(self, app_id, muted):
        self.calls.append(("set_notification_app_muted", app_id, muted))
        if self.mute_error is not None:
            raise self.mute_error
        return muted

    def set_notification_pinned(self, key, pinned):
        self.calls.append(("set_notification_pinned", key, pinned))
        if self.pin_error is not None:
            raise self.pin_error
        return pinned


def _backend_error(name: str, message: str):
    from blueferry.client import BackendError

    class Cause(Exception):
        def get_dbus_name(self):
            return name

        def get_dbus_message(self):
            return message

    error = BackendError(message)
    error.__cause__ = Cause(message)
    return error


def _record(key, app_id, **flags):
    return {"key": key, "id": 1, "app_id": app_id, "app_name": app_id.rsplit(".", 1)[-1],
            "time": "", "title": "T", "pinned": False, "removed": False,
            "actionable": True, **flags}


def test_bridge_rows_carry_the_shared_pin_fields_and_dim_muted_and_removed_apps() -> None:
    client = NotificationAppsClient()
    client.notification_snapshot = {
        "enabled": True, "content": True, "pins_max": 20,
        "notifications": [
            _record("k1", "com.example.mail", pinned=True),
            _record("k2", "com.example.chat"),
            _record("k3", "com.example.mail", removed=True, actionable=False),
            "not a record",
        ],
    }
    reply = QuickshellBridge(client).dispatch("notifications", {})  # type: ignore[arg-type]
    pinned, muted, removed = reply["rows"]
    assert len(reply["rows"]) == 3
    assert (pinned["key"], pinned["pinned"], pinned["pin_label"]) == ("k1", True, "Unpin")
    assert pinned["pinned_text"] == "Pinned" and pinned["dimmed"] is False
    assert muted["muted"] is True and muted["dimmed"] is True and muted["pin_label"] == "Pin"
    assert removed["removed_text"] == "Removed on iPhone" and removed["dimmed"] is True
    assert removed["muted"] is False and removed["actionable"] is False
    assert reply["pin_limit_reached"] is False
    assert json.loads(json.dumps(reply)) == reply


def test_bridge_keeps_pin_fields_with_their_row_when_a_record_is_skipped(monkeypatch) -> None:
    from blueferry import phone_link_view

    shared_rows = phone_link_view.notification_rows
    monkeypatch.setattr(phone_link_view, "notification_rows", lambda records: [
        row for record, row in zip(records, shared_rows(records), strict=True)
        if record.get("key") != "skipped"
    ])
    client = NotificationAppsClient()
    client.notification_snapshot = {
        "enabled": True, "content": True, "pins_max": 20,
        "notifications": [
            _record("skipped", "com.example.chat"),
            _record("k2", "com.example.mail", pinned=True),
        ],
    }
    reply = QuickshellBridge(client).dispatch("notifications", {})  # type: ignore[arg-type]
    assert [(row["key"], row["app"], row["pinned"]) for row in reply["rows"]] == [
        ("k2", "mail", True),
    ]


def test_bridge_reports_the_pin_limit_once_the_list_is_full() -> None:
    client = NotificationAppsClient()
    client.notification_snapshot = {
        "enabled": True, "content": True, "pins_max": 2,
        "notifications": [
            _record("a", "com.example.mail", pinned=True),
            _record("b", "com.example.mail", pinned=True),
            _record("c", "com.example.mail"),
        ],
    }
    reply = QuickshellBridge(client).dispatch("notifications", {})  # type: ignore[arg-type]
    assert reply["pin_limit_reached"] is True
    assert reply["pin_limit_text"] == "At most 2 notifications can be pinned; unpin one first."


def test_bridge_does_not_read_muted_apps_from_a_daemon_without_muting() -> None:
    client = NotificationAppsClient()
    del client.status_value["notification_muted_app_count"]
    client.notification_snapshot = {
        "enabled": True, "content": True,
        "notifications": [_record("k", "com.example.chat")],
    }
    reply = QuickshellBridge(client).dispatch("notifications", {})  # type: ignore[arg-type]
    assert reply["rows"][0]["muted"] is False
    assert ("notification_apps",) not in client.calls


def test_bridge_keeps_the_list_when_the_muted_apps_cannot_be_read() -> None:
    from blueferry.client import BackendError

    client = NotificationAppsClient()
    client.apps = BackendError("locked")
    client.notification_snapshot = {
        "enabled": True, "content": True,
        "notifications": [_record("k", "com.example.chat")],
    }
    reply = QuickshellBridge(client).dispatch("notifications", {})  # type: ignore[arg-type]
    assert reply["failed"] is False and reply["rows"][0]["dimmed"] is False


def test_bridge_pins_a_notification_by_key() -> None:
    client = NotificationAppsClient()
    bridge = QuickshellBridge(client)  # type: ignore[arg-type]
    assert bridge.dispatch("set_notification_pinned", {"key": "k1", "pinned": True}) == {
        "saved": True, "pinned": True, "error": "",
    }
    assert client.calls[-1] == ("set_notification_pinned", "k1", True)
    for args, message in (
        ({"pinned": True}, "key must be a string"),
        ({"key": "k1", "pinned": "yes"}, "pinned must be a boolean"),
    ):
        with pytest.raises(RequestError, match=message):
            bridge.dispatch("set_notification_pinned", args)
    assert len([call for call in client.calls if call[0] == "set_notification_pinned"]) == 1


@pytest.mark.parametrize(("name", "message", "text"), [
    ("io.weirdware.BlueFerry.Error.InvalidArgs", "at most 20 notifications can be pinned",
     "The pinned list is full; unpin a notification first."),
    ("io.weirdware.BlueFerry.Error.InvalidArgs", "unknown notification key",
     "This notification is no longer in the list."),
    ("io.weirdware.BlueFerry.Error.NotReady", "storage is locked",
     "Local data is locked. Unlock it to pin notifications."),
    ("org.freedesktop.DBus.Error.UnknownMethod", "no such method",
     "The running BlueFerry service cannot pin notifications; restart it."),
])
def test_bridge_words_a_refused_pin_with_the_shared_texts(name, message, text) -> None:
    client = NotificationAppsClient()
    client.pin_error = _backend_error(name, message)
    reply = QuickshellBridge(client).dispatch(  # type: ignore[arg-type]
        "set_notification_pinned", {"key": "k1", "pinned": True}
    )
    assert reply == {"saved": False, "pinned": False, "error": text}


def test_bridge_shows_why_a_pin_was_refused_while_settings_cannot_be_read() -> None:
    # SetNotificationPinned answers Configuration while settings.json can't be
    # read; the shared text names the file and the fix, as in the other clients.
    client = NotificationAppsClient()
    message = (
        "settings.json could not be read, so the change was not saved; "
        "repair or remove the file and try again"
    )
    client.pin_error = _backend_error("io.weirdware.BlueFerry.Error.Configuration", message)
    reply = QuickshellBridge(client).dispatch(  # type: ignore[arg-type]
        "set_notification_pinned", {"key": "k1", "pinned": False}
    )
    assert reply == {
        "saved": False, "pinned": True,
        "error": notification_pin_view.UNREADABLE_SETTINGS_TEXT,
    }


def test_bridge_lists_the_notification_apps_with_the_shared_wording() -> None:
    client = NotificationAppsClient()
    reply = QuickshellBridge(client).dispatch("notification_apps", {})  # type: ignore[arg-type]
    assert reply["supported"] is True and reply["saved"] is False and reply["error"] == ""
    assert reply["label"] == "Muted apps" and reply["available"] is True
    assert [row["name"] for row in reply["rows"]] == ["Chat", "Mail"]
    assert [row["toggle_label"] for row in reply["rows"]] == ["Unmute", "Mute"]
    assert reply["count"] == 2 and reply["muted_count"] == 1
    assert json.loads(json.dumps(reply)) == reply


def test_bridge_hides_the_muted_apps_for_a_daemon_without_muting() -> None:
    client = NotificationAppsClient()
    del client.status_value["notification_muted_app_count"]
    reply = QuickshellBridge(client).dispatch("notification_apps", {})  # type: ignore[arg-type]
    assert reply["supported"] is False and reply["rows"] == []
    assert ("notification_apps",) not in client.calls


def test_bridge_explains_unreadable_notification_apps() -> None:
    from blueferry.client import BackendError

    client = NotificationAppsClient()
    client.apps = BackendError("storage is locked")
    reply = QuickshellBridge(client).dispatch("notification_apps", {})  # type: ignore[arg-type]
    assert reply["supported"] is True and reply["rows"] == []
    assert reply["error"] == "storage is locked"


def test_bridge_toggles_the_app_at_the_index_after_rereading_the_list() -> None:
    client = NotificationAppsClient()
    bridge = QuickshellBridge(client)  # type: ignore[arg-type]
    reply = bridge.dispatch(
        "toggle_notification_app", {"index": 1, "app_id": "com.example.mail"}
    )
    assert client.calls[-2:] == [
        ("notification_apps",), ("set_notification_app_muted", "com.example.mail", True),
    ]
    assert reply["saved"] is True and reply["error"] == ""
    assert [row["muted"] for row in reply["rows"]] == [True, True]
    assert reply["muted_count"] == 2


def test_bridge_does_not_toggle_when_the_list_changed_underneath() -> None:
    client = NotificationAppsClient()
    # Another app appeared at index 0 since the page read the list.
    client.apps["apps"].insert(0, {  # type: ignore[index]
        "app_id": "com.example.alpha", "app_name": "Alpha", "muted": False, "seen": True,
    })
    reply = QuickshellBridge(client).dispatch(  # type: ignore[arg-type]
        "toggle_notification_app", {"index": 1, "app_id": "com.example.mail"}
    )
    assert reply["saved"] is False and "changed" in reply["error"]
    assert [row["name"] for row in reply["rows"]] == ["Alpha", "Chat", "Mail"]
    assert not any(call[0] == "set_notification_app_muted" for call in client.calls)


def test_bridge_does_not_mute_a_disabled_row_and_reports_refusals() -> None:
    from blueferry.client import BackendError

    client = NotificationAppsClient()
    client.apps["mute_available"] = False  # type: ignore[index]
    bridge = QuickshellBridge(client)  # type: ignore[arg-type]
    blocked = bridge.dispatch("toggle_notification_app", {"index": 1, "app_id": "com.example.mail"})
    assert blocked["saved"] is False and blocked["error"] == "Could not change the muted apps."
    assert not any(call[0] == "set_notification_app_muted" for call in client.calls)

    client.apps["mute_available"] = True  # type: ignore[index]
    client.mute_error = BackendError("at most 100 apps can be muted")
    refused = bridge.dispatch("toggle_notification_app", {"index": 1, "app_id": "com.example.mail"})
    assert refused["saved"] is False and refused["error"] == "at most 100 apps can be muted"
    assert [row["muted"] for row in refused["rows"]] == [True, False]


def test_a_mute_refused_while_settings_cannot_be_read_shows_muting_unavailable() -> None:
    # settings.json broke between the list read and the write: the daemon
    # refuses with Configuration and now lists mute_available false.
    client = NotificationAppsClient()
    message = (
        "settings.json could not be read, so the change was not saved; "
        "repair or remove the file and try again"
    )
    error = _backend_error("io.weirdware.BlueFerry.Error.Configuration", message)

    def refuse(app_id, muted):
        client.calls.append(("set_notification_app_muted", app_id, muted))
        client.apps["mute_available"] = False  # type: ignore[index]
        raise error

    client.set_notification_app_muted = refuse  # type: ignore[method-assign]
    bridge = QuickshellBridge(client)  # type: ignore[arg-type]
    reply = bridge.dispatch("toggle_notification_app", {"index": 1, "app_id": "com.example.mail"})
    assert reply["saved"] is False and reply["error"] == message
    assert reply["available"] is False
    assert [row["enabled"] for row in reply["rows"]] == [False, False]
    assert [row["muted"] for row in reply["rows"]] == [True, False]
    assert "settings.json can't be read" in reply["hint"]

    # A reread that fails too keeps the list read before the write.
    client.apps["mute_available"] = True  # type: ignore[index]

    def refuse_and_break(app_id, muted):
        client.calls.append(("set_notification_app_muted", app_id, muted))
        client.apps = error
        raise error

    client.set_notification_app_muted = refuse_and_break  # type: ignore[method-assign]
    reply = bridge.dispatch("toggle_notification_app", {"index": 1, "app_id": "com.example.mail"})
    assert reply["error"] == message and [row["name"] for row in reply["rows"]] == ["Chat", "Mail"]


@pytest.mark.parametrize(("args", "message"), [
    ({"app_id": "com.example.mail"}, "index must be an integer"),
    ({"index": True, "app_id": "com.example.mail"}, "index must be an integer"),
    ({"index": 0}, "app_id must be a string"),
])
def test_bridge_rejects_malformed_toggle_requests(args, message) -> None:
    client = NotificationAppsClient()
    with pytest.raises(RequestError, match=message):
        QuickshellBridge(client).dispatch("toggle_notification_app", args)  # type: ignore[arg-type]
    assert client.calls == []


def test_concurrent_toggles_reread_the_list_one_at_a_time() -> None:
    client = NotificationAppsClient()
    active = 0
    overlap = []
    lock = threading.Lock()
    original = client.notification_apps

    def slow_apps():
        nonlocal active
        with lock:
            active += 1
            overlap.append(active)
        time.sleep(0.02)
        try:
            return original()
        finally:
            with lock:
                active -= 1

    client.notification_apps = slow_apps  # type: ignore[method-assign]
    bridge = QuickshellBridge(client)  # type: ignore[arg-type]
    threads = [
        threading.Thread(target=bridge.dispatch, args=(
            "toggle_notification_app", {"index": 1, "app_id": "com.example.mail"},
        ))
        for _ in range(3)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert not any(thread.is_alive() for thread in threads)
    assert max(overlap) == 1


def test_bridge_forwards_call_history_changes_without_content(monkeypatch) -> None:
    # The panel's search shows call hits, so it searches again on this event.
    from blueferry import quickshell_bridge
    from blueferry.protocol import EVENTS_IFACE

    receivers: dict[str, tuple] = {}

    class Bus:
        def add_signal_receiver(self, handler, signal_name, **keywords):
            receivers[signal_name] = (handler, keywords)
            return signal_name

    monkeypatch.setattr(quickshell_bridge, "get_session_bus", lambda: Bus())
    output = io.StringIO()
    bridge = QuickshellBridge(FakeClient(), output)  # type: ignore[arg-type]
    quickshell_bridge._install_signal_receivers(bridge)
    handler, keywords = receivers["CallHistoryChanged"]
    assert keywords["dbus_interface"] == EVENTS_IFACE
    handler()
    assert json.loads(output.getvalue()) == {"event": "call-history-changed", "data": None}


def test_bridge_forwards_the_content_free_notification_apps_signal(monkeypatch) -> None:
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
    bridge._muted_cache = frozenset({"com.example.chat"})
    receivers["NotificationAppsChanged"]()
    assert json.loads(output.getvalue()) == {"event": "notification-apps-changed", "data": None}
    # The event is sent after the cached muted apps are dropped.
    assert bridge._muted_cache is None


def test_list_refreshes_reuse_the_muted_apps_until_they_change() -> None:
    client = NotificationAppsClient()
    client.notification_snapshot = {
        "enabled": True, "content": True,
        "notifications": [_record("k", "com.example.chat")],
    }
    bridge = QuickshellBridge(client)  # type: ignore[arg-type]

    def refresh():
        return bridge.dispatch("notifications", {})["rows"][0]["muted"]

    def app_reads():
        return client.calls.count(("notification_apps",))

    assert refresh() is True and refresh() is True
    assert app_reads() == 1
    client.apps["apps"][0]["muted"] = False  # type: ignore[index]
    bridge.notification_apps_changed()
    assert refresh() is False and refresh() is False
    assert app_reads() == 2
    # The Settings page's read refreshes the cache too.
    client.apps["apps"][0]["muted"] = True  # type: ignore[index]
    bridge.notification_apps_changed()
    bridge.dispatch("notification_apps", {})
    assert refresh() is True and app_reads() == 3


def test_a_failed_muted_apps_read_is_retried_on_the_next_refresh() -> None:
    from blueferry.client import BackendError

    client = NotificationAppsClient()
    client.notification_snapshot = {
        "enabled": True, "content": True,
        "notifications": [_record("k", "com.example.chat")],
    }
    apps = client.apps
    client.apps = BackendError("locked")
    bridge = QuickshellBridge(client)  # type: ignore[arg-type]
    assert bridge.dispatch("notifications", {})["rows"][0]["muted"] is False
    client.apps = apps
    assert bridge.dispatch("notifications", {})["rows"][0]["muted"] is True
    assert client.calls.count(("notification_apps",)) == 2


def test_a_muted_apps_read_racing_a_change_is_not_cached() -> None:
    client = NotificationAppsClient()
    client.notification_snapshot = {
        "enabled": True, "content": True,
        "notifications": [_record("k", "com.example.chat")],
    }
    bridge = QuickshellBridge(client)  # type: ignore[arg-type]
    original = client.notification_apps

    def changed_meanwhile():
        payload = original()
        bridge.notification_apps_changed()
        return payload

    client.notification_apps = changed_meanwhile  # type: ignore[method-assign]
    bridge.dispatch("notifications", {})
    client.notification_apps = original  # type: ignore[method-assign]
    bridge.dispatch("notifications", {})
    assert client.calls.count(("notification_apps",)) == 2


def test_a_saved_toggle_drops_the_cached_muted_apps() -> None:
    client = NotificationAppsClient()
    bridge = QuickshellBridge(client)  # type: ignore[arg-type]
    bridge._muted_cache = frozenset()
    bridge.dispatch("toggle_notification_app", {"index": 1, "app_id": "com.example.mail"})
    assert bridge._muted_cache is None


class ContactsClient(FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.stale: set[str] = set()
        self.search_answer: dict = {}

    def contact_cards(self, offset=0, limit=150, query=""):
        self.calls.append(("contact_cards", offset, limit, query))
        return {
            "contacts": [
                {"id": "c1", "name": "<b>Ana</b>", "organization": "ACME",
                 "primary": "+41790000000", "phone_count": 1},
                {"id": "c1", "name": "duplicate"},
                {"name": "no id"},
            ],
            "total": 60, "offset": offset, "query": query,
        }

    def contact(self, contact_id):
        from blueferry.client import ContactNotFoundError

        self.calls.append(("contact", contact_id))
        if contact_id in self.stale:
            raise ContactNotFoundError("gone")
        return {
            "id": contact_id, "name": "Ana", "title": "Boss", "organization": "ACME",
            "phones": [
                {"kind": "mobile", "value": "079 000 00 00", "number": "+41790000000"},
                {"kind": "custom", "label": "Desk", "value": "ext. 12", "number": ""},
            ],
            "emails": [{"kind": "home", "value": "ana@example.com"}],
            "addresses": [{"kind": "work", "street": "Main St 1", "locality": "Bern",
                           "postal_code": "3000"}],
        }, b"\xff\xd8photo"

    def search(self, query, limit=20):
        self.calls.append(("search", query, limit))
        return {**self.search_answer, "query": query}

    def dial(self, number):
        self.calls.append(("dial", number))
        return "/call/1"


def test_bridge_pages_contact_cards_with_the_shared_rows() -> None:
    client = ContactsClient()
    reply = QuickshellBridge(client).dispatch(  # type: ignore[arg-type]
        "contact_cards", {"query": "ana", "offset": 50},
    )
    assert client.calls == [("contact_cards", 50, 50, "ana")]
    assert reply["rows"] == [{
        "id": "c1", "title": "<b>Ana</b>", "subtitle": "ACME · +41790000000",
        "initials": "", "has_photo": False,
    }]
    assert reply["count_text"] == "60 matching contacts"
    assert reply["has_more"] is True and reply["next_offset"] == 51
    assert reply["offset"] == 50


def test_bridge_contact_rows_carry_no_photo_hint() -> None:
    # The panel shows no card photos: a row never says it has one, also
    # while contact photos are off or the choice is unknown.
    client = ContactsClient()
    client.contact_cards = lambda offset, limit, query: {  # type: ignore[method-assign]
        "contacts": [{"id": "c1", "name": "Ana", "has_photo": True}],
        "total": 1, "offset": offset, "query": query,
    }
    reply = QuickshellBridge(client).dispatch(  # type: ignore[arg-type]
        "contact_cards", {"query": "", "offset": 0},
    )
    assert [row["has_photo"] for row in reply["rows"]] == [False]
    assert "photo" not in json.dumps(QuickshellBridge(client).dispatch(  # type: ignore[arg-type]
        "contact_card", {"id": "c1"},
    ))


@pytest.mark.parametrize("args", [
    {"query": "ana"}, {"query": "ana", "offset": -1}, {"query": "ana", "offset": True},
    {"offset": 0}, {"query": 3, "offset": 0},
])
def test_bridge_rejects_malformed_contact_card_requests(args) -> None:
    client = ContactsClient()
    with pytest.raises(RequestError):
        QuickshellBridge(client).dispatch("contact_cards", args)  # type: ignore[arg-type]
    assert client.calls == []


def test_bridge_opens_a_card_as_flat_rows_without_its_photo() -> None:
    client = ContactsClient()
    bridge = QuickshellBridge(client)  # type: ignore[arg-type]
    reply = bridge.dispatch("contact_card", {"id": "c1"})
    assert reply["found"] is True and reply["message"] == ""
    assert reply["title"] == "Ana" and reply["subtitle"] == "Boss · ACME"
    assert "photo" not in json.dumps(reply)
    rows = reply["rows"]
    assert [(row["section"], row["label"], row["value"]) for row in rows] == [
        ("phones", "Mobile", "079 000 00 00"),
        ("phones", "Desk", "ext. 12"),
        ("emails", "Home", "ana@example.com"),
        ("addresses", "Work", "Main St 1\n3000 Bern"),
    ]
    assert [(row["can_call"], row["can_message"]) for row in rows] == [
        (True, True), (False, False), (False, True), (False, False),
    ]
    assert rows[0]["target"] == "+41790000000"
    assert rows[0]["section_title"] == "Phone"

    client.stale.add("old")
    stale = bridge.dispatch("contact_card", {"id": "old"})
    assert stale["found"] is False and stale["rows"] == []
    assert stale["message"] == "This contact changed or was removed. Reload the list."


def test_bridge_flattens_search_hits_with_the_match_and_the_call_number() -> None:
    client = ContactsClient()
    client.search_answer = {
        "limit": 10,
        "messages": {"total": 12, "results": [
            {"thread_key": "t1", "handle": "h1", "thread_name": "Ana", "sender": "Ana",
             "timestamp": "2026-10-01T10:00:00+00:00", "snippet": "see <b>you</b> soon",
             "match_start": 4, "match_length": 10},
        ]},
        "contacts": {"total": 1, "results": [{"id": "c1", "name": "Ana"}]},
        "calls": {"total": 1, "results": [
            {"id": "k1", "direction": "missed", "timestamp": "2026-10-01T09:00:00+00:00",
             "address": "+41790000000", "name": "Ana"},
        ]},
    }
    bridge = QuickshellBridge(client)  # type: ignore[arg-type]
    reply = bridge.dispatch("search", {"query": "ana"})
    assert client.calls == [("search", "ana", 10)]
    message, contact, call = reply["rows"]
    assert message["section"] == "messages" and message["section_title"] == "Messages"
    assert message["section_more"] == "11 more matches"
    assert (message["snippet_before"], message["snippet_match"], message["snippet_after"]) == (
        "see ", "<b>you</b>", " soon",
    )
    assert (message["kind"], message["thread_key"], message["handle"]) == ("message", "t1", "h1")
    assert (contact["kind"], contact["contact_id"]) == ("contact", "c1")
    assert contact["snippet_before"] == "" and contact["snippet_match"] == ""
    assert (call["kind"], call["number"]) == ("call", "+41790000000")
    assert call["subtitle"] == "Missed call · +41790000000"
    assert reply["limit_text"] == "Showing at most 10 results per group."
    assert reply["count"] == 3 and reply["hint"] == ""
    # Nothing nested: every row value is a plain string.
    assert all(isinstance(value, str) for row in reply["rows"] for value in row.values())

    short = bridge.dispatch("search", {"query": "a"})
    assert short["rows"] == [] and "two characters" in short["hint"]
    assert len(client.calls) == 1


def test_bridge_answers_a_full_search_queue_as_busy() -> None:
    from blueferry import search_view

    client = ContactsClient()
    bridge = QuickshellBridge(client)  # type: ignore[arg-type]
    errors = [_backend_error("io.weirdware.BlueFerry.Error.NotReady", "search is busy")]

    def search(query, limit=20):
        if errors:
            raise errors.pop()
        return {"query": query}

    client.search = search  # type: ignore[method-assign]
    busy = bridge.dispatch("search", {"query": "ana"})
    assert busy["busy"] is True and busy["rows"] == []
    assert busy["error"] == search_view.search_busy_text()
    assert "busy" not in bridge.dispatch("search", {"query": "ana"})
    errors.append(_backend_error("io.weirdware.BlueFerry.Error.Failed", "boom"))
    with pytest.raises(Exception, match="boom"):
        bridge.dispatch("search", {"query": "ana"})


def test_bridge_dials_only_digits_and_reports_the_calls_hint() -> None:
    from blueferry.models import BackendStatus

    client = ContactsClient()
    bridge = QuickshellBridge(client)  # type: ignore[arg-type]
    assert bridge.dispatch("dial", {"number": "+41 79 000 00 00"}) == {"call": "/call/1"}
    assert client.calls == [("dial", "+41790000000")]
    with pytest.raises(RequestError, match="digits"):
        bridge.dispatch("dial", {"number": "ext."})
    with pytest.raises(RequestError):
        bridge.dispatch("dial", {})

    client.status = lambda **_kwargs: BackendStatus.from_dict({  # type: ignore[method-assign]
        "daemon": True, "calls_enabled": True, "calls_state": "ready",
        "contact_card_count": 3,
    })
    assert bridge.dispatch("status", {})["dial_hint"] == ""
    client.status = lambda **_kwargs: BackendStatus.from_dict({  # type: ignore[method-assign]
        "daemon": True, "calls_enabled": False, "search_result_limit": 50,
    })
    assert "Calls & Audio" in bridge.dispatch("status", {})["dial_hint"]
    # An older daemon without contact cards or search: no hint.
    client.status = lambda **_kwargs: BackendStatus.from_dict({  # type: ignore[method-assign]
        "daemon": True, "calls_enabled": False,
    })
    assert "dial_hint" not in bridge.dispatch("status", {})
