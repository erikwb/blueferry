from __future__ import annotations

from blueferry.connectivity import Connectivity, ConnectivityState
from blueferry.obex.sessions import ObexSession, SessionError
from blueferry.profile_supervisor import (
    INITIAL_MAP_CONNECT_POLL_SECONDS,
    MAP_RECONNECT_POLL_SECONDS,
    TRANSPORT_FAILURES_BEFORE_RESET,
    ProfileSupervisor,
)


class FakeSessions:
    def __init__(self) -> None:
        self.map = None
        self.pbap = None
        self.lost = None
        self.monitoring = False
        self.opens = 0
        self.closes = 0
        self.remote_closes = []

    def set_on_lost(self, callback) -> None:
        self.lost = callback

    def start_monitoring(self) -> None:
        self.monitoring = True

    def stop_monitoring(self) -> None:
        self.monitoring = False

    def open_all(self) -> None:
        self.opens += 1
        self.map = ObexSession("MAP", "/map")
        self.pbap = ObexSession("PBAP", "/pbap")

    def close_all(self, *, remove_remote: bool = True) -> None:
        self.closes += 1
        self.remote_closes.append(remove_remote)
        self.map = None
        self.pbap = None


class FakeWorker:
    def __init__(self) -> None:
        self.jobs = []

    def submit(self, operation, *, on_success=None, on_error=None):
        self.jobs.append((operation, on_success, on_error))
        return object()

    def succeed(self) -> None:
        operation, success, _failure = self.jobs.pop(0)
        result = operation()
        if success:
            success(result)

    def fail(self, error: Exception) -> None:
        _operation, _success, failure = self.jobs.pop(0)
        assert failure is not None
        failure(error)


def make_supervisor(
    *,
    attempt_pending=None,
    first_attempt=None,
    attempt_ready=None,
    transport_failure=None,
):
    sessions = FakeSessions()
    worker = FakeWorker()
    scheduled = []
    ready = []
    lost = []
    statuses = []
    supervisor = ProfileSupervisor(
        sessions,
        worker,
        Connectivity(),
        on_ready=lambda: ready.append(True),
        on_lost=lost.append,
        on_status=lambda: statuses.append(True),
        on_attempt_pending=attempt_pending,
        on_first_attempt_complete=first_attempt,
        on_transport_failure=transport_failure,
        attempt_ready=attempt_ready,
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 99,
        cancel=lambda _source: True,
    )
    return supervisor, sessions, worker, scheduled, ready, lost, statuses


def test_open_is_serialized_and_readiness_is_edge_triggered() -> None:
    supervisor, sessions, worker, _scheduled, ready, _lost, _statuses = (
        make_supervisor()
    )

    supervisor.start()
    supervisor.open()
    assert len(worker.jobs) == 1
    worker.succeed()

    assert sessions.monitoring is True
    assert supervisor.ready is True
    assert supervisor.connectivity.state is ConnectivityState.READY
    assert ready == [True]
    supervisor.open()
    assert ready == [True]


def test_forbidden_failure_polls_and_retries() -> None:
    supervisor, _sessions, worker, scheduled, _ready, _lost, _statuses = (
        make_supervisor()
    )
    supervisor.start()
    worker.fail(SessionError("org.bluez.obex.Error.Forbidden"))

    assert supervisor.connectivity.state is ConnectivityState.AUTHORIZATION_REQUIRED
    assert scheduled[0][0] == INITIAL_MAP_CONNECT_POLL_SECONDS
    assert scheduled[0][1]() is False
    assert len(worker.jobs) == 1


def test_first_attempt_callback_runs_once_across_failure_and_retry() -> None:
    attempted = []
    supervisor, _sessions, worker, scheduled, _ready, _lost, _statuses = (
        make_supervisor(first_attempt=lambda: attempted.append(True))
    )

    supervisor.start()
    worker.fail(RuntimeError("not authorized yet"))
    assert attempted == [True]

    scheduled[-1][1]()
    worker.succeed()
    assert attempted == [True]


def test_attempt_gate_is_reapplied_for_each_profile_reconnect_cycle() -> None:
    pending = []
    completed = []
    supervisor, sessions, worker, scheduled, _ready, _lost, _statuses = (
        make_supervisor(
            attempt_pending=lambda: pending.append(True),
            first_attempt=lambda: completed.append(True),
        )
    )

    supervisor.start()
    assert pending == [True]
    worker.succeed()
    assert completed == [True]

    assert sessions.lost is not None
    sessions.lost("phone disconnected")
    assert pending == [True, True]
    worker.succeed()
    scheduled[-1][1]()
    worker.fail(RuntimeError("profile unavailable"))

    assert completed == [True, True]

    scheduled[-1][1]()
    worker.succeed()
    assert completed == [True, True]


def test_attempt_gate_stays_closed_while_classic_is_unavailable() -> None:
    classic = {"connected": False}
    pending = []
    completed = []
    supervisor, _sessions, worker, scheduled, _ready, _lost, _statuses = (
        make_supervisor(
            attempt_pending=lambda: pending.append(True),
            first_attempt=lambda: completed.append(True),
            attempt_ready=lambda: classic["connected"],
        )
    )

    supervisor.start()
    worker.fail(RuntimeError("phone out of range"))
    assert pending == [True]
    assert completed == []

    classic["connected"] = True
    scheduled[-1][1]()
    worker.fail(RuntimeError("profile unavailable"))
    assert completed == [True]


def test_map_connection_refusal_is_exposed_and_polls_quickly_until_ready() -> None:
    supervisor, _sessions, worker, scheduled, _ready, _lost, _statuses = (
        make_supervisor()
    )
    supervisor.start()
    error = SessionError(
        "CreateSession(MAP) failed: org.bluez.obex.Error.Failed: "
        "Connection refused (111)"
    )

    worker.fail(error)

    assert supervisor.connectivity.state is ConnectivityState.MAP_CONNECTION_REFUSED
    assert supervisor.connectivity.detail == str(error)
    assert (
        supervisor.connectivity.retry_delay_seconds
        == INITIAL_MAP_CONNECT_POLL_SECONDS
    )
    assert scheduled[-1][0] == INITIAL_MAP_CONNECT_POLL_SECONDS

    scheduled[-1][1]()
    worker.fail(RuntimeError("still offline"))
    assert scheduled[-1][0] == INITIAL_MAP_CONNECT_POLL_SECONDS


def test_repeated_transport_failures_request_one_targeted_recovery() -> None:
    recoveries = []
    supervisor, _sessions, worker, scheduled, _ready, _lost, _statuses = (
        make_supervisor(
            transport_failure=lambda: recoveries.append(True) or True,
        )
    )
    supervisor.start()

    for failure in range(TRANSPORT_FAILURES_BEFORE_RESET):
        worker.fail(
            SessionError(
                "CreateSession(MAP) failed: "
                "org.bluez.obex.Error.Failed: Transport got disconnected"
            )
        )
        if failure < TRANSPORT_FAILURES_BEFORE_RESET - 1:
            scheduled[-1][1]()

    assert recoveries == [True]


def test_profile_retry_waits_for_targeted_classic_recovery() -> None:
    classic = {"ready": True}

    def recover() -> bool:
        classic["ready"] = False
        return True

    supervisor, _sessions, worker, scheduled, _ready, _lost, _statuses = (
        make_supervisor(
            attempt_ready=lambda: classic["ready"],
            transport_failure=recover,
        )
    )
    supervisor.start()
    for failure in range(TRANSPORT_FAILURES_BEFORE_RESET):
        worker.fail(SessionError("Timed out waiting for response"))
        if failure < TRANSPORT_FAILURES_BEFORE_RESET - 1:
            scheduled[-1][1]()

    scheduled[-1][1]()
    assert worker.jobs == []

    classic["ready"] = True
    scheduled[-1][1]()
    assert len(worker.jobs) == 1


def test_nontransport_profile_failures_break_the_recovery_streak() -> None:
    recoveries = []
    supervisor, _sessions, worker, scheduled, _ready, _lost, _statuses = (
        make_supervisor(
            transport_failure=lambda: recoveries.append(True) or True,
        )
    )
    supervisor.start()

    worker.fail(SessionError("Transport got disconnected"))
    scheduled[-1][1]()
    worker.fail(SessionError("Transport got disconnected"))
    scheduled[-1][1]()
    worker.fail(SessionError("org.bluez.obex.Error.Forbidden"))
    for _failure in range(TRANSPORT_FAILURES_BEFORE_RESET - 1):
        scheduled[-1][1]()
        worker.fail(SessionError("Transport got disconnected"))

    assert recoveries == []


def test_map_refusal_never_cycles_the_classic_bearer() -> None:
    recoveries = []
    supervisor, _sessions, worker, scheduled, _ready, _lost, _statuses = (
        make_supervisor(
            transport_failure=lambda: recoveries.append(True) or True,
        )
    )
    supervisor.start()
    error = SessionError(
        "CreateSession(MAP) failed: org.bluez.obex.Error.Failed: "
        "Connection refused (111) after transport got disconnected"
    )

    for failure in range(TRANSPORT_FAILURES_BEFORE_RESET):
        worker.fail(error)
        if failure < TRANSPORT_FAILURES_BEFORE_RESET - 1:
            scheduled[-1][1]()

    assert recoveries == []


def test_loss_discards_once_then_reconnects() -> None:
    supervisor, sessions, worker, scheduled, ready, lost, _statuses = (
        make_supervisor()
    )
    supervisor.start()
    worker.succeed()

    assert sessions.lost is not None
    sessions.lost("obexd exited")
    sessions.lost("duplicate loss")

    assert lost == ["obexd exited"]
    assert len(worker.jobs) == 1
    worker.succeed()
    assert sessions.closes == 1
    assert sessions.remote_closes == [False]
    assert scheduled[-1][0] == MAP_RECONNECT_POLL_SECONDS
    scheduled[-1][1]()
    worker.fail(RuntimeError("still offline"))
    assert scheduled[-1][0] == MAP_RECONNECT_POLL_SECONDS

    scheduled[-1][1]()
    worker.succeed()
    assert ready == [True, True]


def test_explicit_reconnect_still_removes_live_remote_sessions() -> None:
    supervisor, sessions, worker, scheduled, _ready, _lost, _statuses = (
        make_supervisor()
    )
    supervisor.start()
    worker.succeed()

    supervisor.reconnect("system resumed")
    worker.succeed()

    assert sessions.remote_closes == [True]
    assert scheduled[-1][0] == MAP_RECONNECT_POLL_SECONDS


def test_stop_cancels_a_pending_retry_and_ignores_late_callbacks() -> None:
    supervisor, _sessions, worker, scheduled, ready, _lost, _statuses = (
        make_supervisor()
    )
    supervisor.start()
    worker.fail(RuntimeError("offline"))
    callback = scheduled[0][1]

    supervisor.stop()

    assert callback() is False
    assert worker.jobs == []
    assert ready == []
    assert supervisor.connectivity.state is ConnectivityState.STOPPING


def test_loss_during_open_cannot_publish_stale_readiness() -> None:
    supervisor, sessions, worker, _scheduled, ready, lost, _statuses = (
        make_supervisor()
    )
    supervisor.start()
    open_job = worker.jobs.pop(0)

    supervisor.reconnect("adapter reset during open")
    open_result = open_job[0]()
    open_job[1](open_result)

    assert supervisor.ready is False
    assert ready == []
    assert lost == []
    # The serialized cleanup still removes any sessions created by the stale
    # operation before a retry can begin.
    worker.succeed()
    assert sessions.map is None and sessions.pbap is None
