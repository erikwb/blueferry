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


def test_enabling_le_hands_preference_back_before_any_le_dial() -> None:
    state = {"bredr": True, "le": False}
    calls = []
    scheduled = []
    supervisor = BearerSupervisor(
        "/device",
        le_enabled=False,
        read_connected=state.get,
        prefer=lambda kind: calls.append(("prefer", kind)),
        connect=lambda kind, on_success, _on_error: (
            calls.append(("connect", kind)),
            on_success(),
        ),
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
    )

    supervisor.start()
    supervisor.enable_le()

    assert calls == [("prefer", "le")]
    scheduled[-1][1]()
    assert calls == [("prefer", "le"), ("connect", "le")]


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


def test_hold_cancels_in_flight_le_once_but_keeps_a_late_connection() -> None:
    state = {"bredr": True, "le": False}
    connect_callbacks = []
    disconnect_callbacks = []
    observed = []
    preferences = []
    scheduled = []

    supervisor = BearerSupervisor(
        "/device",
        read_connected=state.get,
        connect=lambda kind, on_success, _on_error: connect_callbacks.append(
            (kind, on_success)
        ),
        disconnect=lambda kind, on_success, on_error: disconnect_callbacks.append(
            (kind, on_success, on_error)
        ),
        prefer=preferences.append,
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
    assert [kind for kind, _success, _error in disconnect_callbacks] == ["le"]

    # BlueZ reports that the best-effort cancellation found no live link, but
    # the original asynchronous Connect subsequently wins the race. Keep and
    # publish that observed link instead of issuing a second Disconnect.
    disconnect_callbacks[0][2](RuntimeError("not connected"))
    connect_callbacks[0][1]()
    state["le"] = True
    health_check()

    assert [kind for kind, _success, _error in disconnect_callbacks] == ["le"]
    assert observed == [False, True]

    supervisor.enable_le()
    assert preferences == []
    assert [kind for kind, _callback in connect_callbacks] == ["le"]


def test_untyped_classic_connect_restores_le_preference_before_le_dial() -> None:
    state = {"bredr": False, "le": False}
    calls = []
    scheduled = []
    now = 0.0
    supervisor = BearerSupervisor(
        "/device",
        read_connected=state.get,
        prefer=lambda kind: calls.append(("prefer", kind)),
        connect=lambda kind, on_success, _on_error: (
            calls.append(("connect", kind)),
            on_success(),
        ),
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
        clock=lambda: now,
    )

    supervisor.start()
    assert calls == [
        ("prefer", "bredr"),
        ("connect", "bredr"),
        ("prefer", "le"),
    ]

    state["bredr"] = True
    scheduled[0][1]()
    scheduled[1][1]()
    assert calls == [
        ("prefer", "bredr"),
        ("connect", "bredr"),
        ("prefer", "le"),
        ("connect", "le"),
    ]

    # Hold the link past the dwell window so it counts as a real session and
    # its earlier penalty is forgiven. This tick issues no new calls.
    now = bearer_supervisor.STABLE_CONNECTION_SECONDS + 1.0
    scheduled[0][1]()

    # A later out-of-range cycle repeats the handoff instead of leaving the
    # idle preference on BR/EDR after its untyped bootstrap.
    state["bredr"] = False
    scheduled[0][1]()
    assert calls[-3:] == [
        ("prefer", "bredr"),
        ("connect", "bredr"),
        ("prefer", "le"),
    ]


def test_enabling_le_waits_for_an_outstanding_classic_connect() -> None:
    state = {"bredr": False, "le": False}
    calls = []
    connect_callbacks = []
    scheduled = []
    supervisor = BearerSupervisor(
        "/device",
        le_enabled=False,
        read_connected=state.get,
        prefer=lambda kind: calls.append(("prefer", kind)),
        connect=lambda kind, on_success, _on_error: (
            calls.append(("connect", kind)),
            connect_callbacks.append(on_success),
        ),
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
    )

    supervisor.start()
    supervisor.enable_le()

    assert calls == [("prefer", "bredr"), ("connect", "bredr")]
    state["bredr"] = True
    supervisor.poke()
    assert [delay for delay, _callback in scheduled] == [
        bearer_supervisor.POLL_SECONDS
    ]

    connect_callbacks[0]()
    assert calls == [
        ("prefer", "bredr"),
        ("connect", "bredr"),
        ("prefer", "le"),
    ]

    supervisor.poke()
    assert [delay for delay, _callback in scheduled] == [
        bearer_supervisor.POLL_SECONDS,
        bearer_supervisor.CLASSIC_SETTLE_SECONDS,
    ]


def test_inbound_le_retargets_an_outstanding_untyped_classic_connect() -> None:
    state = {"bredr": False, "le": False}
    calls = []
    preferences = []
    observed = []
    supervisor = None

    def connect(kind, on_success, on_error):
        assert supervisor is not None
        calls.append(
            (
                kind,
                supervisor._connect_targeted[kind],
                on_success,
                on_error,
            )
        )

    supervisor = BearerSupervisor(
        "/device",
        le_enabled=False,
        read_connected=state.get,
        connect=connect,
        prefer=preferences.append,
        on_le_state=observed.append,
        schedule=lambda _delay, _callback: 7,
    )
    supervisor.start()

    assert [(kind, targeted) for kind, targeted, *_callbacks in calls] == [
        ("bredr", False)
    ]
    assert preferences == ["bredr"]

    state["le"] = True
    supervisor.poke()

    assert observed == [False, True]
    assert [(kind, targeted) for kind, targeted, *_callbacks in calls] == [
        ("bredr", False),
        ("bredr", True),
    ]
    assert preferences == ["bredr"]
    assert supervisor._failures["bredr"] == 0
    assert supervisor._next_attempt["bredr"] == 0.0

    # The obsolete Device1.Connect callback cannot settle or delay the new
    # Bearer.BREDR1.Connect request.
    calls[0][2]()
    assert "bredr" in supervisor._connecting
    assert supervisor._failures["bredr"] == 0
    assert supervisor._next_attempt["bredr"] == 0.0


def test_untyped_classic_already_connected_rechecks_bearers_before_success() -> None:
    import dbus

    state = {"bredr": False, "le": False}
    calls = []
    preferences = []
    supervisor = None

    def connect(kind, on_success, on_error):
        assert supervisor is not None
        calls.append(
            (
                kind,
                supervisor._connect_targeted[kind],
                on_success,
                on_error,
            )
        )

    supervisor = BearerSupervisor(
        "/device",
        le_enabled=False,
        read_connected=state.get,
        connect=connect,
        prefer=preferences.append,
        schedule=lambda _delay, _callback: 7,
    )
    supervisor.start()

    state["le"] = True
    calls[0][3](
        dbus.exceptions.DBusException(
            "Already connected",
            name="org.bluez.Error.AlreadyConnected",
        )
    )

    assert [(kind, targeted) for kind, targeted, *_callbacks in calls] == [
        ("bredr", False),
        ("bredr", True),
    ]
    assert preferences == ["bredr"]
    assert supervisor.bredr_connected is False
    assert supervisor.le_connected is True
    assert supervisor._failures["bredr"] == 0
    assert supervisor._next_attempt["bredr"] == 0.0


def test_le_settle_retries_failed_preference_handoff_without_blocking_dial() -> None:
    state = {"bredr": True, "le": False}
    calls = []
    scheduled = []

    def prefer(kind):
        calls.append(("prefer", kind))
        raise RuntimeError("temporary D-Bus failure")

    supervisor = BearerSupervisor(
        "/device",
        le_enabled=False,
        read_connected=state.get,
        prefer=prefer,
        connect=lambda kind, _on_success, _on_error: calls.append(
            ("connect", kind)
        ),
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
    )

    supervisor.start()
    supervisor.enable_le()
    settle = next(
        callback
        for delay, callback in scheduled
        if delay == bearer_supervisor.CLASSIC_SETTLE_SECONDS
    )
    settle()

    assert calls == [
        ("prefer", "le"),
        ("prefer", "le"),
        ("prefer", "le"),
        ("connect", "le"),
    ]
    assert supervisor._le_preference_restore_pending is True


def test_live_le_dials_classic_without_rewriting_preference() -> None:
    calls = []
    scheduled = []
    supervisor = BearerSupervisor(
        "/device",
        read_connected=lambda kind: {"bredr": False, "le": True}[kind],
        prefer=lambda kind: calls.append(("prefer", kind)),
        connect=lambda kind, *_args: calls.append(("connect", kind)),
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
    )

    supervisor.start()
    scheduled[0][1]()

    assert calls == [("connect", "bredr")]


def test_unknown_le_state_delays_classic_fallback_after_le_is_enabled() -> None:
    calls = []
    supervisor = BearerSupervisor(
        "/device",
        read_connected=lambda kind: {"bredr": False, "le": None}[kind],
        prefer=lambda kind: calls.append(("prefer", kind)),
        connect=lambda kind, *_args: calls.append(("connect", kind)),
        schedule=lambda _delay, _callback: 7,
    )

    supervisor.start()

    assert calls == []


def test_initial_profile_gate_allows_classic_with_no_le_interface() -> None:
    calls = []
    supervisor = BearerSupervisor(
        "/device",
        le_enabled=False,
        read_connected=lambda kind: {"bredr": False, "le": None}[kind],
        prefer=lambda kind: calls.append(("prefer", kind)),
        connect=lambda kind, *_args: calls.append(("connect", kind)),
        schedule=lambda _delay, _callback: 7,
    )

    supervisor.start()

    assert calls == [("prefer", "bredr"), ("connect", "bredr")]


def test_bluez_preference_selects_requested_bearer(monkeypatch) -> None:
    calls = []

    class Properties:
        @staticmethod
        def Get(interface, name, *, timeout):
            calls.append(("get", interface, name, timeout))
            return "bredr"

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
        ("get", "org.bluez.Device1", "PreferredBearer", 5.0),
        ("org.bluez.Device1", "PreferredBearer", "le", 5.0),
    ]


def test_bluez_targets_classic_bearer_when_le_is_live(monkeypatch) -> None:
    calls = []

    class Bearer:
        @staticmethod
        def Connect(**kwargs):
            calls.append(("connect", kwargs["timeout"]))

    class Bus:
        @staticmethod
        def get_object(service, path):
            calls.append((service, path))
            return object()

    monkeypatch.setattr(bearer_supervisor, "get_system_bus", Bus)
    monkeypatch.setattr(
        bearer_supervisor.dbus,
        "Interface",
        lambda _object, interface: calls.append(("interface", interface)) or Bearer(),
    )
    supervisor = BearerSupervisor("/device")
    supervisor._states["le"] = True

    supervisor._connect_bluez("bredr", lambda: None, lambda _error: None)

    assert calls == [
        ("org.bluez", "/device"),
        ("interface", "org.bluez.Bearer.BREDR1"),
        ("connect", 45.0),
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
        def Get(_interface, _name, *, timeout):
            raise bearer_supervisor.dbus.exceptions.DBusException(
                "No such property 'PreferredBearer'",
                name="org.bluez.Error.InvalidArguments",
            )

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


def test_bluez_preference_skips_set_when_already_selected(monkeypatch) -> None:
    calls = []

    class Properties:
        @staticmethod
        def Get(_interface, _name, *, timeout):
            calls.append("get")
            return "le"

        @staticmethod
        def Set(_interface, _name, _value, *, timeout):
            calls.append("set")

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

    BearerSupervisor("/device")._prefer_bluez("le")

    assert calls == ["get"]


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

    # A bearer that HOLDS clears the penalty, so the next genuine disconnect is
    # retried promptly instead of inheriting old delays. The link has to survive
    # the dwell window: a connection that drops immediately is a flap, not
    # evidence that the phone is reachable.
    now = 1.0
    state["bredr"] = True
    scheduled[0][1]()
    now = 1.0 + bearer_supervisor.STABLE_CONNECTION_SECONDS
    scheduled[0][1]()
    state["bredr"] = False
    scheduled[0][1]()

    assert attempts == ["bredr", "bredr"]


def test_returning_le_link_retries_classic_without_rewriting_preference() -> None:
    state = {"bredr": False, "le": False}
    attempts = []
    preferences = []
    scheduled = []
    now = 0.0

    def connect(kind, _on_success, on_error):
        attempts.append(kind)
        on_error(RuntimeError("phone absent"))

    supervisor = BearerSupervisor(
        "/device",
        read_connected=state.get,
        connect=connect,
        prefer=preferences.append,
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
        clock=lambda: now,
    )
    supervisor.start()
    assert attempts == ["bredr"]

    # The old retry is ten seconds away. Live LE clears that penalty and the
    # targeted Classic retry may run, but it must not change PreferredBearer
    # underneath the working ANCS link.
    state["le"] = True
    scheduled[0][1]()

    assert attempts == ["bredr", "bredr"]
    assert preferences == ["bredr"]


def test_live_le_keeps_targeted_classic_fallback_bounded() -> None:
    state = {"bredr": False, "le": True}
    attempts = []
    scheduled = []
    now = 0.0

    def connect(kind, _on_success, on_error):
        attempts.append(kind)
        on_error(RuntimeError("not now"))

    supervisor = BearerSupervisor(
        "/device",
        read_connected=state.get,
        connect=connect,
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
        clock=lambda: now,
    )
    supervisor.start()

    for instant in (10.0, 30.0, 60.0, 90.0):
        now = instant
        scheduled[0][1]()

    assert attempts == ["bredr"] * 5
    assert supervisor._next_attempt["bredr"] == 120.0


def test_le_dial_pauses_solicitation_until_async_reply() -> None:
    state = {"bredr": True, "le": False}
    connect_callbacks = []
    dial_states = []
    scheduled = []
    supervisor = BearerSupervisor(
        "/device",
        read_connected=state.get,
        connect=lambda kind, on_success, on_error: connect_callbacks.append(
            (kind, on_success, on_error)
        ),
        on_le_dial=dial_states.append,
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
    )
    supervisor.start()

    scheduled[0][1]()
    assert [item[0] for item in connect_callbacks] == ["le"]
    assert dial_states == [True]

    connect_callbacks[0][1]()
    assert dial_states == [True, False]


def test_le_dial_restores_solicitation_after_failure() -> None:
    state = {"bredr": True, "le": False}
    connect_callbacks = []
    dial_states = []
    scheduled = []
    supervisor = BearerSupervisor(
        "/device",
        read_connected=state.get,
        connect=lambda kind, on_success, on_error: connect_callbacks.append(
            (kind, on_success, on_error)
        ),
        on_le_dial=dial_states.append,
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
    )
    supervisor.start()

    scheduled[0][1]()
    connect_callbacks[0][2](RuntimeError("no route"))

    assert dial_states == [True, False]


def test_le_in_progress_keeps_solicitation_paused_until_connected() -> None:
    import dbus

    state = {"bredr": True, "le": False}
    connect_callbacks = []
    dial_states = []
    scheduled = []
    supervisor = BearerSupervisor(
        "/device",
        read_connected=state.get,
        connect=lambda kind, on_success, on_error: connect_callbacks.append(
            (kind, on_success, on_error)
        ),
        on_le_dial=dial_states.append,
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
    connect_callbacks[0][2](
        dbus.exceptions.DBusException(
            "Operation already in progress",
            name="org.bluez.Error.InProgress",
        )
    )

    assert dial_states == [True]
    assert "le" in supervisor._connecting
    assert supervisor._le_dial_spent is True

    # Connected=false is expected while BlueZ still owns the operation. The
    # ordinary five-second health poll must not reopen solicitation.
    health_check()
    assert dial_states == [True]
    assert "le" in supervisor._connecting

    state["le"] = True
    health_check()

    assert dial_states == [True, False]
    assert "le" not in supervisor._connecting


def test_le_in_progress_times_out_to_solicitation_without_another_dial() -> None:
    import dbus

    state = {"bredr": True, "le": False}
    connect_callbacks = []
    dial_states = []
    scheduled = []
    now = 0.0
    supervisor = BearerSupervisor(
        "/device",
        read_connected=state.get,
        connect=lambda kind, on_success, on_error: connect_callbacks.append(
            (kind, on_success, on_error)
        ),
        on_le_dial=dial_states.append,
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
        clock=lambda: now,
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
    connect_callbacks[0][2](
        dbus.exceptions.DBusException(
            "Operation already in progress",
            name="org.bluez.Error.InProgress",
        )
    )
    health_check()

    assert dial_states == [True]
    assert "le" in supervisor._connecting
    assert supervisor._le_dial_spent is True

    now = bearer_supervisor.CONNECT_TIMEOUT_SECONDS - 1
    health_check()
    assert dial_states == [True]
    assert "le" in supervisor._connecting

    now = bearer_supervisor.CONNECT_TIMEOUT_SECONDS
    health_check()
    assert dial_states == [True, False]
    assert "le" not in supervisor._connecting
    assert supervisor._le_dial_spent is True
    assert supervisor._failures["le"] == 1

    now = 600.0
    health_check()
    assert [item[0] for item in connect_callbacks] == ["le"]
    assert dial_states == [True, False]


def test_classic_in_progress_times_out_with_exponential_backoff() -> None:
    import dbus

    state = {"bredr": False, "le": False}
    connect_callbacks = []
    scheduled = []
    now = 0.0
    supervisor = BearerSupervisor(
        "/device",
        le_enabled=False,
        read_connected=state.get,
        connect=lambda kind, on_success, on_error: connect_callbacks.append(
            (kind, on_success, on_error)
        ),
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
        clock=lambda: now,
    )
    supervisor.start()
    health_check = next(
        callback
        for delay, callback in scheduled
        if delay == bearer_supervisor.POLL_SECONDS
    )
    connect_callbacks[0][2](
        dbus.exceptions.DBusException(
            "Operation already in progress",
            name="org.bluez.Error.InProgress",
        )
    )

    health_check()
    assert "bredr" in supervisor._connecting

    now = bearer_supervisor.CONNECT_TIMEOUT_SECONDS
    health_check()
    assert "bredr" not in supervisor._connecting
    assert supervisor._failures["bredr"] == 1
    assert supervisor._next_attempt["bredr"] == 55.0
    assert [item[0] for item in connect_callbacks] == ["bredr"]

    now = 54.0
    health_check()
    assert [item[0] for item in connect_callbacks] == ["bredr"]

    now = 55.0
    health_check()
    assert [item[0] for item in connect_callbacks] == ["bredr", "bredr"]


def test_le_already_connected_restores_solicitation_immediately() -> None:
    import dbus

    connect_callbacks = []
    dial_states = []
    scheduled = []
    supervisor = BearerSupervisor(
        "/device",
        read_connected=lambda kind: {"bredr": True, "le": False}[kind],
        connect=lambda kind, on_success, on_error: connect_callbacks.append(
            (kind, on_success, on_error)
        ),
        on_le_dial=dial_states.append,
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
    )
    supervisor.start()
    scheduled[0][1]()

    connect_callbacks[0][2](
        dbus.exceptions.DBusException(
            "Already connected",
            name="org.bluez.Error.AlreadyConnected",
        )
    )

    assert dial_states == [True, False]
    assert "le" not in supervisor._connecting
    assert supervisor._le_dial_spent is True


def test_le_outbound_dial_is_spent_once_then_solicitation_takes_over() -> None:
    state = {"bredr": True, "le": False}
    connections = []
    scheduled = []
    now = 0.0

    supervisor = BearerSupervisor(
        "/device",
        read_connected=state.get,
        connect=lambda kind, on_success, _on_error: (
            connections.append(kind),
            on_success(),
        ),
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
        clock=lambda: now,
    )
    supervisor.start()
    settle = scheduled[0][1]
    health_check = scheduled[1][1]

    settle()
    assert connections == ["le"]

    # Even after the old exponential delay has elapsed, a missing LE bearer
    # does not spend another outbound dial.  The ANCS solicitation remains the
    # durable reconnect mechanism.
    now = 600.0
    health_check()
    assert connections == ["le"]


def test_real_le_disconnect_refunds_one_outbound_dial() -> None:
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
    initial_settle = scheduled[0][1]
    health_check = scheduled[1][1]
    initial_settle()
    assert connections == ["le"]

    state["le"] = True
    health_check()
    state["le"] = False
    health_check()
    settle = next(
        callback
        for delay, callback in reversed(scheduled)
        if delay == bearer_supervisor.CLASSIC_SETTLE_SECONDS
    )
    settle()

    assert connections == ["le", "le"]

    # The lifecycle event grants exactly one attempt. A phone that remains
    # absent is still left to solicitation instead of being dialled forever.
    health_check()
    assert connections == ["le", "le"]


def test_classic_return_refunds_le_dial_spent_while_phone_was_absent() -> None:
    state = {"bredr": True, "le": True}
    le_connections = []
    scheduled = []

    def connect(kind, on_success, _on_error):
        if kind == "le":
            le_connections.append(kind)
        on_success()

    supervisor = BearerSupervisor(
        "/device",
        read_connected=state.get,
        connect=connect,
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
    )
    supervisor.start()
    health_check = scheduled[0][1]

    state["le"] = False
    health_check()
    first_settle = next(
        callback
        for delay, callback in reversed(scheduled)
        if delay == bearer_supervisor.CLASSIC_SETTLE_SECONDS
    )
    first_settle()
    assert le_connections == ["le"]

    state["bredr"] = False
    health_check()
    state["bredr"] = True
    health_check()
    return_settle = next(
        callback
        for delay, callback in reversed(scheduled)
        if delay == bearer_supervisor.CLASSIC_SETTLE_SECONDS
    )
    return_settle()

    assert le_connections == ["le", "le"]


def test_le_outbound_dial_retries_when_solicitation_is_unavailable() -> None:
    state = {"bredr": True, "le": False}
    connections = []
    scheduled = []
    now = 0.0

    supervisor = BearerSupervisor(
        "/device",
        read_connected=state.get,
        connect=lambda kind, on_success, _on_error: (
            connections.append(kind),
            on_success(),
        ),
        inbound_le_primed=lambda: False,
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
        clock=lambda: now,
    )
    supervisor.start()
    settle = scheduled[0][1]
    health_check = scheduled[1][1]

    settle()
    assert connections == ["le"]

    # If BlueZ cannot keep the solicitation advertisement registered, retain
    # the ordinary bounded outbound fallback instead of stranding ANCS.
    now = 11.0
    health_check()
    scheduled[-1][1]()
    assert connections == ["le", "le"]


def test_ancs_transport_reset_refunds_one_le_dial() -> None:
    state = {"bredr": True, "le": False}
    connections = []
    disconnects = []
    scheduled = []

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
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
    )
    supervisor.start()
    scheduled[0][1]()
    assert connections == ["le"]

    state["le"] = True
    scheduled[1][1]()
    supervisor.recover_le_transport()
    assert disconnects == ["le"]

    state["le"] = False
    scheduled[1][1]()
    next_settle = next(
        callback
        for delay, callback in reversed(scheduled)
        if delay == bearer_supervisor.CLASSIC_SETTLE_SECONDS
    )
    next_settle()

    assert connections == ["le", "le"]


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
    disconnect_callbacks = []
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
            disconnect_callbacks.append(on_success),
        ),
        on_le_state=observed.append,
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
        clock=lambda: now,
    )
    supervisor.start()

    supervisor.recover_le_transport()
    supervisor.recover_le_transport()
    assert disconnects == ["le"]

    state["le"] = False
    disconnect_callbacks[0]()
    scheduled[0][1]()
    assert disconnects == ["le"]
    assert observed == [True, False]
    assert scheduled[-1][0] == bearer_supervisor.CLASSIC_SETTLE_SECONDS

    scheduled[-1][1]()
    assert connections == ["le"]


def test_gatt_transport_reset_preserves_a_reconnect_between_polls() -> None:
    state = {"bredr": True, "le": True}
    disconnects = []
    observed = []
    scheduled = []

    supervisor = BearerSupervisor(
        "/device",
        read_connected=state.get,
        connect=lambda *_args: None,
        disconnect=lambda kind, on_success, _on_error: (
            disconnects.append(kind),
            on_success(),
        ),
        on_le_state=observed.append,
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
        cancel=lambda _timer_id: None,
    )
    supervisor.start()

    # The iPhone can reconnect before the five-second health poll. The
    # successful reset reply must still publish the generation boundary so
    # ANCS can discard its blocked transport and rebuild on the live bearer.
    supervisor.recover_le_transport()
    supervisor.recover_le_transport()
    assert disconnects == ["le"]
    assert observed == [True, False]

    health_check = next(
        callback
        for delay, callback in scheduled
        if delay == bearer_supervisor.POLL_SECONDS
    )
    health_check()

    assert observed == [True, False, True]
    assert disconnects == ["le"]
    assert supervisor._le_reset_pending is False


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


def test_bluez_restart_accepts_new_le_link_while_profile_gate_is_closed() -> None:
    state = {"bredr": True, "le": True}
    connections = []
    disconnects = []
    observed = []
    preferences = []
    now = 0.0
    supervisor = None

    def connect(kind, _on_success, on_error):
        assert supervisor is not None
        connections.append((kind, supervisor._connect_targeted[kind], on_error))

    supervisor = BearerSupervisor(
        "/device",
        read_connected=state.get,
        connect=connect,
        disconnect=lambda kind, _on_success, _on_error: disconnects.append(kind),
        prefer=preferences.append,
        on_le_state=observed.append,
        schedule=lambda _delay, _callback: 7,
        clock=lambda: now,
    )
    supervisor.start()
    supervisor.hold_le()
    state["bredr"] = False

    supervisor.reset_after_bluez_restart()

    assert disconnects == []
    assert [(kind, targeted) for kind, targeted, _error in connections] == [
        ("bredr", True)
    ]
    assert preferences == []
    assert observed == [True, None, True]
    assert supervisor.le_connected is True

    # A transient read failure is published normally; no teardown or untyped
    # Classic fallback is introduced while the targeted request is in flight.
    state["le"] = None
    supervisor.poke()
    assert preferences == []
    assert disconnects == []
    assert observed == [True, None, True, None]

    supervisor._failures["bredr"] = 9
    connections[0][2](RuntimeError("not now"))
    assert supervisor._next_attempt["bredr"] == (
        bearer_supervisor.LE_CONNECTED_CLASSIC_BACKOFF_CAP_SECONDS
    )

    now = bearer_supervisor.LE_CONNECTED_CLASSIC_BACKOFF_CAP_SECONDS
    supervisor.poke()
    assert [(kind, targeted) for kind, targeted, _error in connections] == [
        ("bredr", True),
        ("bredr", True),
    ]
    assert preferences == []


def test_inbound_le_during_hold_resets_and_caps_classic_backoff() -> None:
    state = {"bredr": True, "le": False}
    connect_callbacks = []
    scheduled = []
    now = 0.0
    supervisor = BearerSupervisor(
        "/device",
        le_enabled=False,
        read_connected=state.get,
        connect=lambda kind, _on_success, on_error: connect_callbacks.append(
            (kind, on_error)
        ),
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
        clock=lambda: now,
    )
    supervisor.start()
    supervisor._failures["bredr"] = 6
    supervisor._next_attempt["bredr"] = 300.0

    state.update(bredr=False, le=True)
    scheduled[0][1]()

    assert supervisor.le_connected is True
    assert supervisor._failures["bredr"] == 0
    assert supervisor._next_attempt["bredr"] == 0.0
    assert [kind for kind, _on_error in connect_callbacks] == ["bredr"]

    supervisor._failures["bredr"] = 9
    connect_callbacks[0][1](RuntimeError("not now"))
    assert supervisor._next_attempt["bredr"] == (
        bearer_supervisor.LE_CONNECTED_CLASSIC_BACKOFF_CAP_SECONDS
    )


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


def test_flapping_bearer_does_not_clear_backoff() -> None:
    """A phone that connects then drops at once must not pin the retry delay.

    Connected=true is observed on every five-second poll, not just on genuine
    transitions, so clearing the failure counter on each observation let a
    flapping device reset the backoff before it could ever grow. The retry
    interval stayed pinned at its five-second minimum forever.
    """
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

    # The bearer appears and drops again well inside the dwell window.
    now = 1.0
    state["bredr"] = True
    scheduled[0][1]()
    now = 2.0
    state["bredr"] = False
    scheduled[0][1]()

    # The 10s penalty earned by the first failure must still be in force.
    assert attempts == ["bredr"]

    # ...and once it genuinely expires, the retry proceeds as normal.
    now = 11.0
    scheduled[0][1]()
    assert attempts == ["bredr", "bredr"]


def test_backoff_grows_across_repeated_flaps() -> None:
    """Repeated connect/drop cycles must escalate the retry delay.

    Reproduces an observed field failure: a phone whose bearer flapped
    continuously kept every retry pinned at ten seconds -- POLL_SECONDS * 2**1
    -- because each Connected=true observation zeroed the failure counter
    before it could ever grow past one. The five-minute ceiling was therefore
    unreachable and the phone was re-dialled every ten seconds indefinitely.
    """
    state = {"bredr": False, "le": False}
    attempts = []
    scheduled = []
    now = 0.0

    def connect(_kind, _on_success, on_error):
        attempts.append(now)
        on_error(RuntimeError("not ready"))

    supervisor = BearerSupervisor(
        "/device",
        read_connected=state.get,
        connect=connect,
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
        cancel=lambda _timer_id: None,
        clock=lambda: now,
    )

    def flap(at: float) -> None:
        """A bearer that appears and drops again inside the dwell window."""
        nonlocal now
        now = at
        state["bredr"] = True
        scheduled[0][1]()
        now = at + 1.0
        state["bredr"] = False
        scheduled[0][1]()

    supervisor.start()
    assert attempts == [0.0]

    # First failure earns a 10s penalty; a flap must not forgive it.
    flap(1.0)
    now = 11.0
    scheduled[0][1]()
    assert attempts == [0.0, 11.0]

    # The second failure escalates to 20s. Another flap must not reset that
    # either -- under the old behaviour the delay collapsed back to 10s here.
    flap(12.0)
    now = 25.0
    scheduled[0][1]()
    assert attempts == [0.0, 11.0]

    now = 32.0
    scheduled[0][1]()
    assert attempts == [0.0, 11.0, 32.0]
