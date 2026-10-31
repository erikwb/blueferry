"""The opt-in Notifications tab state shared by the clients: no toolkit."""
from __future__ import annotations

import pytest

from blueferry.notification_history import LIMIT, REFRESH_DELAY_MS, NotificationHistory
from tests.client_fakes import ManualRunner

SNAPSHOT = {"enabled": True, "content": False, "notifications": [
    {"id": 1, "app_id": "com.example.chat", "app_name": "",
     "time": "2026-10-06T10:00:00+00:00"},
]}


def _history(work: ManualRunner, answer=lambda: SNAPSHOT):
    calls: list[int] = []
    changes: list[None] = []

    def fetch(limit):
        calls.append(limit)
        return answer()

    history = NotificationHistory(runner=work, timer=work.timer, fetch=fetch,
                                  on_change=lambda: changes.append(None))
    history.sync_status({"daemon": True, "notification_history_enabled": True,
                         "notification_content_shown": True, "storage_state": "ready"})
    changes.clear()
    return history, calls, changes


def test_fetched_only_while_watched_and_forgotten_after() -> None:
    work = ManualRunner()
    history, calls, changes = _history(work)
    history.refresh()
    history.invalidated()
    work.drain()
    assert calls == [] and changes == []

    history.watch(True)
    assert calls == [] and work.pending  # on the worker
    work.run_tasks()
    assert calls == [LIMIT]
    assert history.rows[0]["app"] == "com.example.chat" and history.rows[0]["body"] == ""
    assert history.info == {"enabled": True, "content": False, "error": ""}

    changes.clear()
    history.watch(False)
    assert history.rows == [] and changes == [None]
    history.watch(False)  # nothing left to forget
    assert changes == [None]


def test_invalidations_coalesce_into_one_reload_of_a_shown_list() -> None:
    work = ManualRunner()
    history, calls, _changes = _history(work)
    history.watch(True)
    work.run_tasks()
    history.invalidated()
    history.invalidated()
    assert history._timer.delay == REFRESH_DELAY_MS and history._timer.starts == 2
    work.drain()
    assert calls == [LIMIT, LIMIT]


def test_hiding_the_tab_cancels_a_pending_reload_and_drops_a_late_answer() -> None:
    work = ManualRunner()
    history, calls, _changes = _history(work)
    history.watch(True)
    history.watch(False)  # hidden before the answer
    work.run_tasks()
    assert calls == [LIMIT] and history.rows == []
    history.watch(True)
    work.run_tasks()
    history.invalidated()
    history.watch(False)
    assert not history._timer.active


def test_a_failure_stays_in_the_tab() -> None:
    def locked():
        raise RuntimeError("local history is locked")

    work = ManualRunner()
    history, _calls, changes = _history(work, locked)
    history.watch(True)
    work.run_tasks()
    assert history.rows == [] and changes
    assert history.info["error"] == "local history is locked"
    history._fetch = lambda _limit: (_ for _ in ()).throw(RuntimeError(""))
    history.refresh()
    work.run_tasks()
    assert history.info["error"] == "Notifications are unavailable"


@pytest.mark.parametrize("change", [
    {"daemon": False}, {"storage_state": "locked"},
    {"notification_content_shown": False}, {"phone_profile_id": "another-phone"},
])
@pytest.mark.parametrize("fails", [False, True])
def test_inflight_reply_cannot_cross_privacy_or_profile_change(change, fails):
    def answer():
        if fails:
            raise RuntimeError("old phone error")
        return {"enabled": True, "content": True, "notifications": [
            {"app_id": "chat", "body": "old phone body"},
        ]}

    work = ManualRunner()
    history, _calls, _changes = _history(work, answer)
    history.watch(True)
    reply = work.start_next()
    history.sync_status({"daemon": True, "notification_history_enabled": True,
                         "notification_content_shown": True, "storage_state": "ready", **change})
    reply()
    assert history.rows == [] and history.info["error"] == ""
    # At most one fresh read follows; a locked/unavailable backend gets none.
    assert len(work.pending) == (0 if not history._available() else 1)


def test_refresh_storm_coalesces_and_tab_reopen_rejects_old_response():
    work = ManualRunner()
    history, calls, _changes = _history(work)
    history.watch(True)
    reply = work.start_next()
    for _ in range(30):
        history.refresh()
    assert not work.pending and calls == [LIMIT]
    history.watch(False)
    history.watch(True)
    reply()
    assert history.rows == [] and len(work.pending) == 1
    work.run_tasks()
    assert calls == [LIMIT, LIMIT] and history.rows
