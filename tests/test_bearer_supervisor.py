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
    assert supervisor.snapshot() == {
        "bredr": True,
        "le": True,
        "last_le_error": "",
        "last_le_error_message": "",
    }
    assert "probing iPhone BR/EDR and LE bearer state" in caplog.text
    assert "iPhone BREDR bearer state: disconnected" in caplog.text
    assert "iPhone BREDR bearer state: connected" in caplog.text
    assert "iPhone LE bearer state: connected" in caplog.text


def test_snapshot_includes_the_last_le_connect_error() -> None:
    import dbus

    state = {"bredr": True, "le": False}

    def connect(kind, on_success, on_error):
        if kind == "le":
            on_error(
                dbus.exceptions.DBusException(
                    "le-connection-abort-by-local",
                    name="org.bluez.Error.Failed",
                )
            )
            return
        on_success()

    scheduled = []
    supervisor = BearerSupervisor(
        "/device",
        read_connected=state.get,
        connect=connect,
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
    )
    supervisor.start()
    scheduled[0][1]()
    assert supervisor.snapshot() == {
        "bredr": True,
        "le": False,
        "last_le_error": "org.bluez.Error.Failed",
        "last_le_error_message": "connection-aborted",
    }


def test_snapshot_does_not_expose_device_paths_from_bluez_errors() -> None:
    supervisor = BearerSupervisor(
        "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF",
        read_connected=lambda _kind: False,
        connect=lambda *_args: None,
    )
    supervisor._last_errors["le"] = (
        "org.bluez.Error.Failed",
        "failure at /org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF",
    )

    snapshot = supervisor.snapshot()

    assert snapshot["last_le_error"] == "org.bluez.Error.Failed"
    assert snapshot["last_le_error_message"] == "connection-failed"
    assert "dev_AA" not in str(snapshot)


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


def test_bluez_preference_ignores_a_missing_preferred_bearer_property(monkeypatch) -> None:
    class Properties:
        @staticmethod
        def Set(_interface, _name, _value, *, timeout):
            raise bearer_supervisor.dbus.exceptions.DBusException(
                "No such property 'PreferredBearer'",
                name="org.bluez.Error.InvalidArguments",
            )

    class Bus:
        @staticmethod
        def get_object(_service, _path):
            return object()

    monkeypatch.setattr(bearer_supervisor, "get_system_bus", Bus)
    monkeypatch.setattr(
        bearer_supervisor.dbus,
        "Interface",
        lambda _object, _interface: Properties(),
    )

    BearerSupervisor("/device")._prefer_bluez("bredr")


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


def test_le_observer_receives_every_polled_state() -> None:
    state = {"bredr": True, "le": True}
    observed = []
    scheduled = []
    supervisor = BearerSupervisor(
        "/device",
        read_connected=state.get,
        connect=lambda *_args: None,
        on_le_state=observed.append,
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
    )

    supervisor.start()
    assert supervisor.le_state is True
    assert observed == [True]

    # Repeat observations let a consumer recover when it detected a brief
    # disconnect that fell entirely between supervisor polls.
    scheduled[0][1]()
    assert observed == [True, True]

    state["le"] = False
    scheduled[0][1]()
    assert supervisor.le_state is False
    assert observed[-1] is False


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
