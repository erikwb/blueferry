"""Plugin API 1.5: content-free popups and the bounded plugin service."""
from __future__ import annotations

import json
import threading

import pytest

from blueferry.plugin_api import (
    METHOD_GET_PENDING_NOTIFICATION,
    SIGNAL_NOTIFY_CHANGED,
)
from blueferry.plugin_api import surfaces as sf
from blueferry.plugin_api.client import PluginClient, PluginError
from blueferry.plugin_api.service import (
    MAX_CALLERS,
    MAX_IN_FLIGHT,
    MAX_WORKERS,
    NotifyService,
    PluginService,
    RateLimitedError,
    emit_notify,
)
from blueferry.plugin_api.testing import FakeHost, ScriptedTransport, inline_service, manifest

NOTIFY = manifest("io.example.notify", capabilities="notify;", api_version="1.5")


def _signal_signature(cls, name: str) -> str:
    return getattr(cls, name)._dbus_signature


def test_the_popup_signal_carries_nothing() -> None:
    assert SIGNAL_NOTIFY_CHANGED == "NotifyChanged"
    assert _signal_signature(NotifyService, "NotifyChanged") == ""
    # The 1.2 signal with the popup text on the bus is gone.
    assert not hasattr(NotifyService, "Notify")
    method = NotifyService.GetPendingNotification
    assert method._dbus_interface == "io.weirdware.BlueFerry.Plugin1"
    assert METHOD_GET_PENDING_NOTIFICATION == method.__name__


def test_pending_popups_come_out_oldest_first_and_once() -> None:
    signals: list[str] = []
    service = inline_service(NotifyService, NOTIFY)
    service.NotifyChanged = lambda: signals.append("changed")
    emit_notify(service, "One", "first", "view-calendar", "Open", "open")
    emit_notify(service, "Two", "second")
    emit_notify(service, "", "")  # nothing to show: neither queued nor signalled
    assert signals == ["changed", "changed"]
    first = json.loads(service.GetPendingNotification(sender=":1.2"))
    assert first == {"notification": {"title": "One", "body": "first", "icon": "view-calendar",
                                      "action_label": "Open", "action_id": "open"},
                     "more": True}
    second = json.loads(service.GetPendingNotification(sender=":1.2"))
    assert second["notification"]["title"] == "Two" and second["more"] is False
    assert json.loads(service.GetPendingNotification(sender=":1.2")) == {
        "notification": None, "more": False}


def test_the_queue_is_short_and_stale_popups_expire() -> None:
    now = [0.0]
    service = inline_service(NotifyService, NOTIFY, clock=lambda: now[0])
    service.NotifyChanged = lambda: None
    for number in range(sf.MAX_PENDING_NOTIFICATIONS + 3):
        service.emit_notify(f"P{number}", "x")
    first = json.loads(service.GetPendingNotification(sender=":1.2"))
    assert first["notification"]["title"] == "P3"  # the oldest three were dropped
    now[0] = sf.PENDING_NOTIFICATION_TTL_SEC + 1
    assert json.loads(service.GetPendingNotification(sender=":1.2"))["notification"] is None


def test_fake_host_fetches_like_the_daemon() -> None:
    service = inline_service(NotifyService, NOTIFY)
    host = FakeHost(service)
    service.emit_notify("Reminder", "Dentist", "", "Open", "open")
    assert [note.title for note in host.notifications] == ["Reminder"]
    assert [call[1] for call in host.transport.calls] == ["GetPendingNotification"]


@pytest.mark.parametrize("reply", [
    "{nope", "[]", json.dumps({"notification": "x"}), json.dumps({"more": 1}),
])
def test_the_client_refuses_broken_pending_replies(reply) -> None:
    client = PluginClient(NOTIFY, transport=ScriptedTransport({
        "GetPendingNotification": reply}))
    with pytest.raises(PluginError):
        client.pending_notification()


def test_the_client_cleans_a_pending_popup() -> None:
    client = PluginClient(NOTIFY, transport=ScriptedTransport({
        "GetPendingNotification": json.dumps({"notification": {
            "title": "<b>T\x1b[2J</b>", "body": "b" * 900, "icon": "../evil",
            "action_label": "Go", "action_id": "bad id"}, "more": False})}))
    note, more = client.pending_notification()
    assert note is not None and not more
    assert note.title == "<b>T [2J</b>" and len(note.body) == sf.MAX_NOTIFY_BODY
    assert note.icon == "" and not note.has_action


def test_one_shot_callers_do_not_grow_the_budget_table() -> None:
    now = [0.0]
    service = inline_service(PluginService, NOTIFY, clock=lambda: now[0])
    for number in range(MAX_CALLERS + 50):
        service.Status(sender=f":1.{number}")
    assert len(service._calls) <= MAX_CALLERS
    now[0] = 120.0
    service.Status(sender=":2.1")
    assert list(service._calls) == [":2.1"]


def test_too_many_unanswered_calls_are_refused() -> None:
    held: list = []
    service = PluginService(NOTIFY, to_main=lambda callback: callback(),
                            start_worker=held.append)
    replies: list = []
    for _ in range(MAX_IN_FLIGHT):
        service.run_async(lambda: "{}", replies.append, replies.append)
    with pytest.raises(RateLimitedError):
        service.admit(":1.4")
    held.pop()()
    service.admit(":1.4")
    assert replies == ["{}"]


def test_the_default_worker_pool_is_bounded() -> None:
    service = PluginService(NOTIFY, to_main=lambda callback: callback())
    release = threading.Event()
    started = threading.Semaphore(0)
    running: list[int] = []
    lock = threading.Lock()
    peak = [0]

    def work() -> str:
        with lock:
            running.append(1)
            peak[0] = max(peak[0], len(running))
        started.release()
        release.wait(5)
        with lock:
            running.pop()
        return "{}"

    done = threading.Semaphore(0)
    for _ in range(MAX_WORKERS + 3):
        service.run_async(work, lambda _reply: done.release(), lambda _error: done.release())
    for _ in range(MAX_WORKERS):
        assert started.acquire(timeout=5)
    assert not started.acquire(timeout=0.2)  # the rest waits for a free worker
    release.set()
    for _ in range(MAX_WORKERS + 3):
        assert done.acquire(timeout=5)
    assert peak[0] == MAX_WORKERS
    service._executor.shutdown(wait=True)
