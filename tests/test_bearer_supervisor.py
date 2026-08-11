"""Bearer supervision tests; no Bluetooth or D-Bus access is permitted."""

from __future__ import annotations

import logging

from blueferry import bearer_supervisor
from blueferry.bearer_supervisor import BearerSupervisor


def test_connects_classic_before_le(caplog) -> None:
    state = {"bredr": False, "le": False}
    connections = []
    scheduled = []
    caplog.set_level(logging.DEBUG, logger="blueferry.bearer_supervisor")

    def connect(kind, on_success, _on_error):
        connections.append(kind)
        on_success()

    supervisor = BearerSupervisor(
        "/device",
        read_connected=state.get,
        connect=connect,
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
    )

    supervisor.start()
    assert connections == ["bredr"]

    state["bredr"] = True
    scheduled[0][1]()
    assert connections == ["bredr"]
    assert scheduled[1][0] == 3

    scheduled[1][1]()
    assert connections == ["bredr", "le"]

    state["le"] = True
    scheduled[0][1]()
    assert supervisor.snapshot() == {"bredr": True, "le": True}
    assert "probing iPhone BR/EDR and LE bearer state" in caplog.text
    assert "iPhone BREDR bearer state: disconnected" in caplog.text
    assert "iPhone BREDR bearer state: connected" in caplog.text
    assert "iPhone LE bearer state: connected" in caplog.text


def test_le_is_not_connected_if_classic_drops_during_settling() -> None:
    state = {"bredr": True, "le": False}
    connections = []
    scheduled = []
    supervisor = BearerSupervisor(
        "/device",
        read_connected=state.get,
        connect=lambda kind, on_success, _on_error: (
            connections.append(kind),
            on_success(),
        ),
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
    )

    supervisor.start()
    assert scheduled[0][0] == 3

    state["bredr"] = False
    scheduled[0][1]()

    assert connections == ["bredr"]
    assert "le" not in connections


def test_le_can_be_held_until_classic_profile_attempt_finishes() -> None:
    state = {"bredr": True, "le": False}
    connections = []
    scheduled = []
    supervisor = BearerSupervisor(
        "/device",
        le_enabled=False,
        read_connected=state.get,
        connect=lambda kind, on_success, _on_error: (
            connections.append(kind),
            on_success(),
        ),
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
    )

    supervisor.start()
    assert connections == []
    assert [delay for delay, _callback in scheduled] == [5]

    supervisor.enable_le()
    assert [delay for delay, _callback in scheduled] == [5, 3]
    scheduled[-1][1]()
    assert connections == ["le"]


def test_selects_each_bearer_before_requesting_its_connection() -> None:
    state = {"bredr": False, "le": False}
    calls = []
    scheduled = []
    supervisor = BearerSupervisor(
        "/device",
        read_connected=state.get,
        prefer=lambda kind: calls.append(("prefer", kind)),
        connect=lambda kind, on_success, _on_error: (
            calls.append(("connect", kind)),
            on_success(),
        ),
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
    )

    supervisor.start()
    assert calls == [("prefer", "bredr"), ("connect", "bredr")]

    state["bredr"] = True
    scheduled[0][1]()
    scheduled[1][1]()
    assert calls[-2:] == [("prefer", "le"), ("connect", "le")]


def test_bluez_preference_selects_requested_bearer(monkeypatch) -> None:
    calls = []

    class Properties:
        @staticmethod
        def Set(interface, name, value, *, timeout):
            calls.append((interface, name, str(value), timeout))

    class Bus:
        @staticmethod
        def get_object(service, path):
            calls.append((service, path))
            return object()

    monkeypatch.setattr(bearer_supervisor, "get_system_bus", Bus)
    monkeypatch.setattr(
        bearer_supervisor.dbus,
        "Interface",
        lambda _object, interface: calls.append(("interface", interface)) or Properties(),
    )
    supervisor = BearerSupervisor("/device")

    supervisor._prefer_bluez("le")

    assert calls == [
        ("org.bluez", "/device"),
        ("interface", "org.freedesktop.DBus.Properties"),
        ("org.bluez.Device1", "PreferredBearer", "le", 5.0),
    ]


def test_failed_connection_is_retried_after_backoff() -> None:
    attempts = []
    scheduled = []
    now = 0.0

    def connect(kind, _on_success, on_error):
        attempts.append(kind)
        on_error(RuntimeError("not ready"))

    supervisor = BearerSupervisor(
        "/device",
        read_connected=lambda _kind: False,
        connect=connect,
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
        clock=lambda: now,
    )

    supervisor.start()
    assert attempts == ["bredr"]

    # A rejecting phone must not be hammered on every five-second poll.
    scheduled[0][1]()
    assert attempts == ["bredr"]

    now = 11.0
    scheduled[0][1]()
    assert attempts == ["bredr", "bredr"]

    # The second failure pushes the next attempt out further (20s), and the
    # delay stops growing at the configured ceiling.
    now = 21.0
    scheduled[0][1]()
    assert attempts == ["bredr", "bredr"]
    now = 32.0
    scheduled[0][1]()
    assert attempts == ["bredr", "bredr", "bredr"]


def test_backoff_resets_once_the_bearer_connects() -> None:
    state = {"bredr": False, "le": False}
    attempts = []
    scheduled = []
    now = 0.0

    def connect(kind, _on_success, on_error):
        attempts.append(kind)
        on_error(RuntimeError("not ready"))

    supervisor = BearerSupervisor(
        "/device",
        read_connected=state.get,
        connect=connect,
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
        cancel=lambda _timer_id: None,
        clock=lambda: now,
    )

    supervisor.start()
    assert attempts == ["bredr"]

    # The bearer connecting through any path clears the penalty, so the next
    # genuine disconnect is retried promptly instead of inheriting old delays.
    state["bredr"] = True
    scheduled[0][1]()
    state["bredr"] = False
    scheduled[0][1]()

    assert attempts == ["bredr", "bredr"]


def test_stop_cancels_health_check() -> None:
    cancelled = []
    supervisor = BearerSupervisor(
        "/device",
        read_connected=lambda _kind: None,
        connect=lambda *_args: None,
        schedule=lambda _delay, _callback: 7,
        cancel=cancelled.append,
    )

    supervisor.start()
    supervisor.stop()

    assert cancelled == [7]
