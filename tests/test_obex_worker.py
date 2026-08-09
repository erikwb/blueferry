"""The OBEX executor is serialized and returns through GLib."""
from __future__ import annotations

import threading

import pytest

from blueferry.obex import worker as worker_mod


def test_worker_serializes_operations_and_marshals_completion(monkeypatch):
    initialized = []
    idle_calls = []
    first_started = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    delivered = []
    errors = []

    monkeypatch.setattr(
        worker_mod,
        "initialize_obex_worker_bus",
        lambda: initialized.append(threading.get_ident()),
    )
    monkeypatch.setattr(worker_mod, "close_obex_worker_bus", lambda: None)

    def idle_add(callback, *args):
        idle_calls.append((callback, args))
        return len(idle_calls)

    monkeypatch.setattr(worker_mod.GLib, "idle_add", idle_add)
    worker = worker_mod.ObexWorker()

    def first():
        first_started.set()
        assert release_first.wait(timeout=2)
        return "first"

    def second():
        second_started.set()
        return "second"

    def fail():
        raise RuntimeError("transfer failed")

    try:
        first_future = worker.submit(first, on_success=delivered.append)
        second_future = worker.submit(second, on_success=delivered.append)
        failed_future = worker.submit(fail, on_error=errors.append)

        assert first_started.wait(timeout=2)
        assert not second_started.is_set()
        release_first.set()
        assert first_future.result(timeout=2) == "first"
        assert second_future.result(timeout=2) == "second"
        with pytest.raises(RuntimeError, match="transfer failed"):
            failed_future.result(timeout=2)
        assert second_started.is_set()
        assert len(initialized) == 1
        assert delivered == []
        assert errors == []

        for callback, args in list(idle_calls):
            callback(*args)
        assert delivered == ["first", "second"]
        assert len(errors) == 1
        assert str(errors[0]) == "transfer failed"
    finally:
        release_first.set()
        worker.shutdown()


def test_shutdown_cancels_queued_phone_work_before_cleanup(monkeypatch) -> None:
    monkeypatch.setattr(worker_mod, "initialize_obex_worker_bus", lambda: None)
    monkeypatch.setattr(worker_mod, "close_obex_worker_bus", lambda: None)
    monkeypatch.setattr(worker_mod.GLib, "idle_add", lambda *_args: 1)
    worker = worker_mod.ObexWorker()
    first_started = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    cleaned = threading.Event()

    def first() -> None:
        first_started.set()
        assert release_first.wait(timeout=2)

    def second() -> None:
        second_started.set()

    worker.submit(first)
    second_future = worker.submit(second)
    assert first_started.wait(timeout=2)

    shutdown = threading.Thread(
        target=lambda: worker.shutdown(cleanup=cleaned.set)
    )
    shutdown.start()
    assert second_future.cancelled()
    release_first.set()
    shutdown.join(timeout=2)

    assert not shutdown.is_alive()
    assert not second_started.is_set()
    assert cleaned.is_set()


def test_worker_rejects_an_unbounded_operation_backlog(monkeypatch) -> None:
    monkeypatch.setattr(worker_mod, "MAX_OBEX_PENDING_OPERATIONS", 1)
    monkeypatch.setattr(worker_mod, "initialize_obex_worker_bus", lambda: None)
    monkeypatch.setattr(worker_mod, "close_obex_worker_bus", lambda: None)
    monkeypatch.setattr(worker_mod.GLib, "idle_add", lambda *_args: 1)
    worker = worker_mod.ObexWorker()
    started = threading.Event()
    release = threading.Event()

    def blocked() -> None:
        started.set()
        assert release.wait(timeout=2)

    worker.submit(blocked)
    assert started.wait(timeout=2)
    with pytest.raises(RuntimeError, match="queue is full"):
        worker.submit(lambda: None)
    release.set()
    worker.shutdown()
