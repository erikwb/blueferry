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
    assert connections == ["bredr", "le"]

    state["le"] = True
    scheduled[0][1]()
    assert supervisor.snapshot() == {"bredr": True, "le": True}


def test_failed_connection_is_retried_on_next_health_check() -> None:
    attempts = []
    scheduled = []

    def connect(kind, _on_success, on_error):
        attempts.append(kind)
        on_error(RuntimeError("not ready"))

    supervisor = BearerSupervisor(
        "/device",
        read_connected=lambda _kind: False,
        connect=connect,
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
    )

    supervisor.start()
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
