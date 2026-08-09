"""Bearer supervision tests; no Bluetooth or D-Bus access is permitted."""

from __future__ import annotations

from blueferry.bearer_supervisor import BearerSupervisor


def test_connects_classic_before_le() -> None:
    state = {"bredr": False, "le": False}
    connections = []
    scheduled = []

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
