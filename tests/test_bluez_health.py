"""BlueZ responsiveness: NoReply evidence, Peer.Ping probes, the kernel reason."""
from __future__ import annotations

import logging

import dbus.exceptions

from blueferry.bearer_supervisor import BearerSupervisor
from blueferry.bluez_health import (
    PING_FAILURE_THRESHOLD,
    PING_INTERVAL_SEC,
    PING_INTERVAL_UNRESPONSIVE_SEC,
    BluezHealth,
    IntrospectNoReplyFilter,
    process_state,
)
from blueferry.reconnect_view import BLUEZ_HUNG_TEXT, reconnect_view, result_text

NO_REPLY = dbus.exceptions.DBusException("x", name="org.freedesktop.DBus.Error.NoReply")


class _Harness:
    def __init__(self, *, state: str = "D") -> None:
        self.pings: list[tuple] = []
        self.pid_requests: list = []
        self.changes = 0
        self.timers: list[tuple[int, object]] = []
        self.cancelled: list[int] = []
        self.health = BluezHealth(
            ping=lambda ok, err: self.pings.append((ok, err)),
            owner_pid=self.pid_requests.append,
            read_state=lambda pid: state if pid == 4242 else "",
            on_change=self._changed,
            schedule=lambda delay, callback: self.timers.append((delay, callback)) or len(self.timers),
            cancel=self.cancelled.append,
        )

    def _changed(self) -> None:
        self.changes += 1

    def fail_pings(self) -> None:
        """Let every ping sent so far time out (each may send a follow-up)."""
        pending, self.pings[:] = list(self.pings), []
        for _ok, err in pending:
            err(NO_REPLY)


def test_two_unanswered_pings_mark_bluez_unresponsive_and_explain_it() -> None:
    harness = _Harness()
    health = harness.health
    health.start()
    assert harness.timers[0][0] == PING_INTERVAL_SEC
    health.report(dbus.exceptions.DBusException("x", name="org.bluez.Error.Failed"))
    assert harness.pings == []  # only NoReply/Timeout prompt a ping
    health.report(NO_REPLY)
    health.report(NO_REPLY)
    assert len(harness.pings) == 1  # one early ping, never a storm
    harness.fail_pings()
    assert health.unresponsive is False  # one slow answer is not enough
    assert harness.timers[-1][0] == PING_INTERVAL_UNRESPONSIVE_SEC
    health.report(NO_REPLY)
    assert harness.pings == []  # the confirmation waits for its timer
    harness.timers[-1][1]()
    harness.fail_pings()
    assert health.unresponsive is True and harness.changes == 1
    assert health.snapshot() == {"bluez_unresponsive": True, "bluez_unresponsive_reason": ""}
    harness.timers[-1][1]()
    harness.fail_pings()
    assert harness.changes == 1  # no repeated warnings or status storms
    harness.pid_requests[0](4242)  # /proc says: uninterruptible sleep
    assert health.snapshot()["bluez_unresponsive_reason"] == "kernel"
    # A ping that finally answers resumes everything.
    harness.timers[-1][1]()
    ok, _err = harness.pings.pop()
    ok()
    assert health.unresponsive is False and health.snapshot()["bluez_unresponsive_reason"] == ""
    assert harness.timers[-1][0] == PING_INTERVAL_SEC
    health.stop()


def _make_unresponsive(harness: _Harness) -> None:
    harness.health.start()
    for _ in range(PING_FAILURE_THRESHOLD):
        harness.timers[-1][1]()
        harness.fail_pings()
    assert harness.health.unresponsive


def test_owner_change_clears_the_state_and_ignores_old_pings() -> None:
    harness = _Harness(state="S")
    health = harness.health
    _make_unresponsive(harness)
    harness.pid_requests[0](4242)
    assert health.snapshot()["bluez_unresponsive_reason"] == ""  # not in D state
    health.probe()
    stale_ok, _ = harness.pings.pop()
    health.owner_changed(":1.99")
    assert not health.unresponsive
    stale_ok()  # from the previous bluetoothd generation: ignored
    health.probe()
    assert len(harness.pings) == 1  # a new probe is possible again
    health.stop()


def test_stop_frees_a_pending_ping() -> None:
    harness = _Harness()
    harness.health.start()
    harness.health.probe()
    harness.health.stop()
    harness.health.probe()
    assert len(harness.pings) == 2


def test_process_state_reads_proc_status(tmp_path) -> None:
    (tmp_path / "7").mkdir()
    (tmp_path / "7" / "status").write_text("Name:\tbluetoothd\nState:\tD (disk sleep)\n")
    assert process_state(7, proc=tmp_path) == "D"
    assert process_state(8, proc=tmp_path) == ""


def test_supervisor_stays_idle_while_bluez_is_unresponsive() -> None:
    blocked = [False]
    reads: list[str] = []
    errors: list = []
    ok: list[bool] = []

    def read(kind):
        reads.append(kind)
        raise NO_REPLY

    supervisor = BearerSupervisor(
        "/device", read_connected=read, connect=lambda *_a: None,
        schedule=lambda *_a: 7, cancel=lambda _id: None, clock=lambda: 0.0,
        bluez_blocked=lambda: blocked[0], on_bluez_error=errors.append,
        on_bluez_ok=lambda: ok.append(True),
    )
    supervisor.start()
    assert reads == ["bredr", "le"] and len(errors) == 2
    blocked[0] = True
    supervisor.poke()
    assert len(reads) == 2  # no reads, no dials
    assert supervisor.reconnect_now() == "bluez-unresponsive"
    supervisor.stop()


def test_clients_show_the_restart_hint() -> None:
    view = reconnect_view({
        "daemon": True, "phone_reconnect_state": "waiting",
        "bluez_unresponsive": True, "bluez_unresponsive_reason": "kernel",
    })
    assert view.offered and view.hint == BLUEZ_HUNG_TEXT
    assert result_text("bluez-kernel") == BLUEZ_HUNG_TEXT
    assert "restart the computer" in result_text("bluez-unresponsive").lower()


def test_introspect_no_reply_logs_are_throttled() -> None:
    now = [0.0]
    flt = IntrospectNoReplyFilter(window_sec=600, clock=lambda: now[0])

    def record(message: str) -> logging.LogRecord:
        return logging.LogRecord("dbus.proxies", logging.ERROR, "", 0, message, (), None)

    noisy = "Introspect error on :1.5:/org/bluez: org.freedesktop.DBus.Error.NoReply"
    assert flt.filter(record(noisy)) is True
    assert flt.filter(record(noisy)) is False
    assert flt.filter(record("something else")) is True
    now[0] = 601.0
    again = record(noisy)
    assert flt.filter(again) is True and "1 similar" in again.getMessage()
