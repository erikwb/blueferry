"""The opt-in Notifications tab state shared by the clients: no toolkit."""
from __future__ import annotations

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


def test_one_read_at_a_time_and_an_older_reply_never_wins() -> None:
    answers = iter([
        {"enabled": True, "content": False, "notifications": [
            {"id": 1, "app_name": "Old", "time": "2026-10-06T10:00:00+00:00"}]},
        {"enabled": True, "content": False, "notifications": [
            {"id": 2, "app_name": "New", "time": "2026-10-06T10:01:00+00:00"}]},
    ])
    work = ManualRunner()
    history, calls, changes = _history(work, lambda: next(answers))
    history.watch(True)
    history.refresh()
    history.refresh()  # both coalesce into one read after the running one
    assert work.submitted == 1
    work.run_next()  # the older reply arrives: superseded, not shown
    assert history.rows == [] and changes == []
    assert work.submitted == 2
    work.run_tasks()
    assert [row["app"] for row in history.rows] == ["New"]
    assert calls == [LIMIT, LIMIT]


def test_a_superseded_failure_is_not_shown() -> None:
    results = iter([RuntimeError("old failure"), SNAPSHOT])

    def answer():
        value = next(results)
        if isinstance(value, Exception):
            raise value
        return value

    work = ManualRunner()
    history, _calls, _changes = _history(work, answer)
    history.watch(True)
    history.refresh()
    work.run_next()
    assert history.info["error"] == ""
    work.run_tasks()
    assert history.rows and history.info["error"] == ""


def test_hiding_while_a_read_runs_drops_the_queued_reread() -> None:
    work = ManualRunner()
    history, calls, _changes = _history(work)
    history.watch(True)
    history.refresh()
    history.watch(False)
    work.run_tasks()
    assert calls == [LIMIT] and history.rows == []
    history.watch(True)
    work.run_tasks()
    assert calls == [LIMIT, LIMIT] and history.rows
