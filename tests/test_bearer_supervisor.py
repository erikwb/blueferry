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


def test_le_can_be_held_again_during_a_later_profile_reconnect() -> None:
    state = {"bredr": True, "le": False}
    connections = []
    scheduled = []
    cancelled = []
    supervisor = BearerSupervisor(
        "/device",
        read_connected=state.get,
        connect=lambda kind, on_success, _on_error: (
            connections.append(kind),
            on_success(),
        ),
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
        cancel=cancelled.append,
    )

    supervisor.start()
    assert scheduled[0][0] == bearer_supervisor.CLASSIC_SETTLE_SECONDS

    supervisor.hold_le()
    assert cancelled == [7]
    scheduled[0][1]()
    assert connections == []

    supervisor.enable_le()
    assert scheduled[-1][0] == bearer_supervisor.CLASSIC_SETTLE_SECONDS
    scheduled[-1][1]()
    assert connections == ["le"]


def test_hold_rejects_an_le_connect_already_in_flight() -> None:
    state = {"bredr": True, "le": False}
    connect_callbacks = []
    disconnect_callbacks = []
    observed = []
    scheduled = []

    supervisor = BearerSupervisor(
        "/device",
        read_connected=state.get,
        connect=lambda kind, on_success, _on_error: connect_callbacks.append(
            (kind, on_success)
        ),
        disconnect=lambda kind, on_success, _on_error: disconnect_callbacks.append(
            (kind, on_success)
        ),
        on_le_state=observed.append,
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
    )
    supervisor.start()
    settle = next(
        callback
        for delay, callback in scheduled
        if delay == bearer_supervisor.CLASSIC_SETTLE_SECONDS
    )
    health_check = next(
        callback
        for delay, callback in scheduled
        if delay == bearer_supervisor.POLL_SECONDS
    )
    settle()
    assert [kind for kind, _callback in connect_callbacks] == ["le"]

    supervisor.hold_le()
    connect_callbacks[0][1]()
    state["le"] = True
    health_check()

    # The pending Connect is countered immediately, and its transient True
    # state is not published to ANCS while the profile gate is closed.
    assert [kind for kind, _callback in disconnect_callbacks] == ["le"]
    assert observed == [False]

    disconnect_callbacks[0][1]()
    state["le"] = False
    health_check()
    supervisor.enable_le()
    assert observed == [False]
    next_settle = next(
        callback
        for delay, callback in reversed(scheduled)
        if delay == bearer_supervisor.CLASSIC_SETTLE_SECONDS
    )
    next_settle()
    assert [kind for kind, _callback in connect_callbacks] == ["le", "le"]


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


def test_bluez_reads_classic_bearer_instead_of_aggregate_device_state(
    monkeypatch,
) -> None:
    calls = []

    class Properties:
        @staticmethod
        def Get(interface, name, *, timeout):
            calls.append((interface, name, timeout))
            if interface == "org.bluez.Bearer.BREDR1":
                return False
            if interface == "org.bluez.Device1":
                return True
            raise AssertionError(interface)

    class Bus:
        @staticmethod
        def get_object(service, path):
            calls.append((service, path))
            return object()

    monkeypatch.setattr(bearer_supervisor, "get_system_bus", Bus)
    monkeypatch.setattr(
        bearer_supervisor.dbus,
        "Interface",
        lambda _object, interface: calls.append(("interface", interface))
        or Properties(),
    )

    connected = BearerSupervisor("/device")._read_bluez_connected("bredr")

    assert connected is False
    assert ("org.bluez.Device1", "Connected", 5.0) not in calls


def test_bluez_falls_back_when_classic_bearer_is_marker_only(monkeypatch) -> None:
    calls = []

    class Properties:
        @staticmethod
        def Get(interface, name, *, timeout):
            calls.append((interface, name, timeout))
            if interface == "org.bluez.Bearer.BREDR1":
                raise bearer_supervisor.dbus.exceptions.DBusException(
                    "No such property 'Connected'",
                    name="org.freedesktop.DBus.Error.UnknownProperty",
                )
            return True

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

    connected = BearerSupervisor("/device")._read_bluez_connected("bredr")

    assert connected is True
    assert calls == [
        ("org.bluez.Bearer.BREDR1", "Connected", 5.0),
        ("org.bluez.Device1", "Connected", 5.0),
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


def test_accepted_connection_request_waits_for_observed_state_change() -> None:
    attempts = []
    scheduled = []
    now = 0.0

    supervisor = BearerSupervisor(
        "/device",
        read_connected=lambda _kind: False,
        connect=lambda kind, on_success, _on_error: (
            attempts.append(kind),
            on_success(),
        ),
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
        clock=lambda: now,
    )
    supervisor.start()
    assert attempts == ["bredr"]

    scheduled[0][1]()
    assert attempts == ["bredr"]

    now = 11.0
    scheduled[0][1]()
    assert attempts == ["bredr", "bredr"]


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


def test_le_observer_receives_only_lifecycle_transitions() -> None:
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

    # A stale true must not make ANCS resubscribe after a failed GATT request.
    scheduled[0][1]()
    assert observed == [True]

    state["le"] = False
    scheduled[0][1]()
    assert supervisor.le_state is False
    assert observed[-1] is False


def test_gatt_transport_failure_cycles_le_once_before_reconnecting() -> None:
    state = {"bredr": True, "le": True}
    disconnects = []
    connections = []
    observed = []
    scheduled = []
    now = 0.0

    supervisor = BearerSupervisor(
        "/device",
        read_connected=state.get,
        connect=lambda kind, on_success, _on_error: (
            connections.append(kind),
            on_success(),
        ),
        disconnect=lambda kind, on_success, _on_error: (
            disconnects.append(kind),
            on_success(),
        ),
        on_le_state=observed.append,
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
        clock=lambda: now,
    )
    supervisor.start()

    supervisor.recover_le_transport()
    supervisor.recover_le_transport()
    assert disconnects == ["le"]

    scheduled[0][1]()
    assert disconnects == ["le"]

    state["le"] = False
    scheduled[0][1]()
    assert observed == [True, False]
    assert scheduled[-1][0] == bearer_supervisor.CLASSIC_SETTLE_SECONDS

    scheduled[-1][1]()
    assert connections == ["le"]


def test_gatt_transport_recovery_waits_for_profile_gate_to_reopen() -> None:
    state = {"bredr": True, "le": True}
    disconnects = []
    supervisor = BearerSupervisor(
        "/device",
        read_connected=state.get,
        disconnect=lambda kind, on_success, _on_error: (
            disconnects.append(kind),
            on_success(),
        ),
        schedule=lambda _delay, _callback: 7,
    )
    supervisor.start()

    supervisor.hold_le()
    supervisor.recover_le_transport()
    assert disconnects == []

    supervisor.enable_le()
    assert disconnects == ["le"]


def test_bluez_restart_rejects_new_le_link_while_profile_gate_is_closed() -> None:
    state = {"bredr": True, "le": True}
    connections = []
    disconnects = []
    observed = []
    supervisor = BearerSupervisor(
        "/device",
        read_connected=state.get,
        connect=lambda kind, _on_success, _on_error: connections.append(kind),
        disconnect=lambda kind, _on_success, _on_error: disconnects.append(kind),
        on_le_state=observed.append,
        schedule=lambda _delay, _callback: 7,
    )
    supervisor.start()
    supervisor.hold_le()
    state["bredr"] = False

    supervisor.reset_after_bluez_restart()

    assert disconnects == ["le"]
    assert connections == ["bredr"]
    assert observed == [True, None]


def test_bluez_restart_discards_callbacks_from_the_previous_owner() -> None:
    callbacks = []
    statuses = []
    observed = []

    def connect(_kind, on_success, on_error):
        callbacks.append((on_success, on_error))

    supervisor = BearerSupervisor(
        "/device",
        read_connected=lambda _kind: False,
        connect=connect,
        on_status=lambda: statuses.append(True),
        on_le_state=observed.append,
        schedule=lambda _delay, _callback: 7,
    )
    supervisor.start()
    assert len(callbacks) == 1

    supervisor.reset_after_bluez_restart()
    callbacks[0][1](RuntimeError("stale failure"))

    assert supervisor.snapshot()["last_le_error"] == ""
    assert observed[-1] is False
    assert statuses


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
