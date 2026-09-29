"""Pure transfer-state tests; no BlueZ or OBEX connection is opened."""
from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import dbus
import pytest

from blueferry.errors import SendOutcomeUnknownError
from blueferry.obex import transfer
from blueferry.obex.transfer import TransferFailed, TransferStatusWatch, wait_for_transfer


@pytest.fixture(autouse=True)
def cancel(monkeypatch):
    interface = Mock()
    monkeypatch.setattr(transfer, "obex", lambda *_args: interface)
    return interface.Cancel


def test_worker_waits_for_main_thread_subscription_and_removes_watch_there(monkeypatch):
    scheduled = []
    pending = threading.Event()
    ready = threading.Event()
    release = threading.Event()
    calls = []
    result = {}

    def schedule(callback):
        scheduled.append(callback)
        pending.set()

    def subscribe(callback, **_kwargs):
        calls.append(('subscribe', threading.get_ident()))
        result['callback'] = callback
        return SimpleNamespace(remove=lambda: calls.append(('remove', threading.get_ident())))

    monkeypatch.setattr(transfer.GLib, 'idle_add', schedule)
    monkeypatch.setattr(transfer, '_add_transfer_receiver', subscribe)

    def run():
        try:
            watch = TransferStatusWatch('/session')
            result['watch'] = watch
            ready.set()
            assert release.wait(5)
            watch.close()
        except Exception as error:
            result['error'] = error
            ready.set()

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert pending.wait(5)
        assert not ready.is_set()
        assert scheduled.pop(0)() is False
        assert ready.wait(5)
        assert 'error' not in result
        result['callback']('org.bluez.obex.Transfer1', {'Status': 'complete'}, [], path='/session/transfer1')
        assert result['watch'].terminal('/session/transfer1') == 'complete'
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert calls == [('subscribe', threading.get_ident())]
    assert scheduled.pop(0)() is False
    assert calls == [('subscribe', threading.get_ident()), ('remove', threading.get_ident())]


def test_timed_out_watch_never_subscribes_when_glib_resumes(monkeypatch):
    scheduled = []
    errors = []
    monkeypatch.setattr(transfer.GLib, 'idle_add', scheduled.append)
    monkeypatch.setattr(transfer, '_WATCH_SETUP_TIMEOUT_S', 0.01)
    get_bus = Mock(side_effect=AssertionError('late subscription'))
    monkeypatch.setattr(transfer, 'get_session_bus', get_bus)

    def run():
        try:
            TransferStatusWatch('/session')
        except Exception as error:
            errors.append(error)

    worker = threading.Thread(target=run)
    worker.start()
    worker.join(5)
    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], TimeoutError)
    for callback in scheduled:
        assert callback() is False
    get_bus.assert_not_called()


def test_watch_timeout_during_subscription_removes_late_match_on_main_thread(monkeypatch):
    scheduled = []
    pending = threading.Event()
    subscribing = threading.Event()
    finished = threading.Event()
    removed = []
    errors = []

    def schedule(callback):
        scheduled.append(callback)
        pending.set()
        assert subscribing.wait(5)

    def subscribe(*_args, **_kwargs):
        subscribing.set()
        assert finished.wait(5)
        return SimpleNamespace(remove=lambda: removed.append(threading.get_ident()))

    monkeypatch.setattr(transfer.GLib, 'idle_add', schedule)
    monkeypatch.setattr(transfer, '_WATCH_SETUP_TIMEOUT_S', 0.01)
    monkeypatch.setattr(transfer, '_add_transfer_receiver', subscribe)

    def run():
        try:
            TransferStatusWatch('/session')
        except Exception as error:
            errors.append(error)
        finally:
            finished.set()

    worker = threading.Thread(target=run)
    worker.start()
    assert pending.wait(5)
    assert scheduled.pop(0)() is False
    worker.join(5)
    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], TimeoutError)
    assert removed == [threading.get_ident()]
    assert scheduled.pop(0)() is False
    assert removed == [threading.get_ident()]


def test_subscription_failure_reaches_worker_without_waiting_for_timeout(monkeypatch):
    scheduled = []
    pending = threading.Event()
    errors = []
    original = RuntimeError('session bus unavailable')

    def schedule(callback):
        scheduled.append(callback)
        pending.set()

    monkeypatch.setattr(transfer.GLib, 'idle_add', schedule)
    monkeypatch.setattr(transfer, 'get_session_bus', Mock(side_effect=original))

    def run():
        try:
            TransferStatusWatch('/session')
        except Exception as error:
            errors.append(error)

    worker = threading.Thread(target=run)
    worker.start()
    assert pending.wait(5)
    assert scheduled.pop(0)() is False
    worker.join(5)
    assert not worker.is_alive()
    assert errors == [original]


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_wait_requires_terminal_completion(cancel) -> None:
    statuses = iter(["queued", "active", "complete"])
    clock = _Clock()

    assert wait_for_transfer(
        "/transfer/1", timeout_s=5, get_status=lambda: next(statuses),
        monotonic=clock, sleep=clock.sleep,
    ) == "complete"
    cancel.assert_not_called()


def test_nonterminal_status_at_deadline_is_not_success(cancel) -> None:
    clock = _Clock()

    with pytest.raises(TimeoutError, match="last status: active"):
        wait_for_transfer(
            "/transfer/2", timeout_s=0.2, get_status=lambda: "active",
            monotonic=clock, sleep=clock.sleep,
        )
    cancel.assert_called_once_with(timeout=2.0)


def test_transfer_timeout_restarts_when_progress_advances() -> None:
    clock = _Clock()
    polls = 0

    def status() -> str:
        nonlocal polls
        polls += 1
        return "complete" if polls == 8 else "active"

    assert wait_for_transfer(
        "/transfer/long",
        timeout_s=0.2,
        get_status=status,
        get_progress=lambda: int(clock.now / 0.2),
        monotonic=clock,
        sleep=clock.sleep,
    ) == "complete"
    assert clock.now > 0.2


def test_progress_regression_does_not_restart_inactivity_timeout() -> None:
    clock = _Clock()

    with pytest.raises(TimeoutError, match=r"timed out after 0\.2s"):
        wait_for_transfer(
            "/transfer/regressing",
            timeout_s=0.2,
            get_status=lambda: "active",
            get_progress=lambda: 10 if clock.now < 0.1 else 9,
            monotonic=clock,
            sleep=clock.sleep,
        )


def test_progress_cannot_extend_overall_timeout(cancel) -> None:
    clock = _Clock()

    with pytest.raises(TimeoutError, match=r"0\.5s overall limit"):
        wait_for_transfer(
            "/transfer/slow-loris",
            timeout_s=0.2,
            overall_timeout_s=0.5,
            get_status=lambda: "active",
            get_progress=lambda: int(clock.now * 100),
            monotonic=clock,
            sleep=clock.sleep,
        )
    cancel.assert_called_once_with(timeout=2.0)


def test_explicit_transfer_error_fails() -> None:
    with pytest.raises(TransferFailed):
        wait_for_transfer(
            "/transfer/3", initial_status="error", timeout_s=1,
        )


@pytest.mark.parametrize("initial_status", ["queued", "active"])
def test_disappearance_requires_independent_download_verification(cancel, initial_status) -> None:
    def gone():
        raise dbus.exceptions.DBusException(
            "gone", name="org.freedesktop.DBus.Error.UnknownObject",
        )

    with pytest.raises(SendOutcomeUnknownError):
        wait_for_transfer(
            "/transfer/4", timeout_s=1, get_status=gone, initial_status=initial_status,
        )
    assert wait_for_transfer(
        "/transfer/4", timeout_s=1, get_status=gone, allow_disappearance=True,
    ) == "gone"
    cancel.assert_not_called()


def test_unrelated_dbus_failure_is_not_treated_as_completion(cancel) -> None:
    def disconnected():
        raise dbus.exceptions.DBusException(
            "lost", name="org.freedesktop.DBus.Error.Disconnected",
        )

    with pytest.raises(RuntimeError, match="Disconnected"):
        wait_for_transfer(
            "/transfer/5", timeout_s=1, get_status=disconnected,
        )
    cancel.assert_called_once_with(timeout=2.0)


@pytest.mark.parametrize("callback", ["check_progress", "get_progress"])
@pytest.mark.parametrize("fail_after", [0, 2])
def test_progress_rejection_survives_failed_cancellation(cancel, callback, fail_after):
    clock = _Clock()
    original = RuntimeError("size limit")
    calls = 0
    cancel.side_effect = dbus.exceptions.DBusException("transfer already gone")

    def progress():
        nonlocal calls
        calls += 1
        if calls > fail_after:
            raise original
        return 0

    with pytest.raises(RuntimeError) as caught:
        wait_for_transfer(
            "/transfer/oversized", timeout_s=1, get_status=lambda: "active",
            monotonic=clock, sleep=clock.sleep, **{callback: progress},
        )

    assert caught.value is original
    cancel.assert_called_once_with(timeout=2.0)
