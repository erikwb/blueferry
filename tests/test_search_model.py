"""The shared global search state (blueferry.search_model), without a toolkit."""
from __future__ import annotations

import json

import dbus

from blueferry import search_view
from blueferry.client import BackendError
from blueferry.search_model import BUSY_RETRIES, LIMIT, GlobalSearch
from tests.test_contacts_model import Runner, Timer

STATUS = {"search_result_limit": 50}


def _answer(query):
    return json.dumps({
        "query": query, "limit": LIMIT,
        "conversations": {"total": 1, "available": True, "results": [
            {"thread_key": "t1", "name": "Ada", "is_group": False,
             "timestamp": "2026-10-30T10:00:00Z"}]},
        "messages": {"total": 1, "available": True, "results": [
            {"thread_key": "t1", "handle": "h9", "thread_name": "Ada",
             "sender": "Ada", "outgoing": False, "timestamp": "2026-10-30T10:00:00Z",
             "snippet": "see you at noon", "match_start": 11, "match_length": 4}]},
        "contacts": {"total": 1, "available": True, "results": [
            {"id": "c1", "name": "Ada Lovelace", "organization": "", "primary": ""}]},
        "calls": {"total": 1, "available": True, "results": [
            {"id": "call-1", "direction": "missed", "timestamp": "2026-10-30T09:00:00Z",
             "address": "+15550001", "name": "Ada", "contact_name": "Ada"}]},
    })


class Daemon:
    def __init__(self) -> None:
        self.queries: list = []
        self.busy = 0
        self.fail = ""
        self.cap = 0

    def search(self, query, limit):
        self.queries.append((query, limit))
        if self.busy:
            self.busy -= 1
            error = dbus.exceptions.DBusException(
                "search is busy", name="io.weirdware.BlueFerry1.Error.NotReady")
            raise BackendError("search is busy") from error
        if self.fail:
            raise BackendError(self.fail)
        if self.cap:
            return json.dumps({**json.loads(_answer(query)), "limit": self.cap})
        return _answer(query)


def _search(status=STATUS):
    daemon, runner, timers, changes = Daemon(), Runner(), [], []

    def timer(callback):
        timers.append(Timer(callback))
        return timers[-1]

    model = GlobalSearch(runner=runner, timer=timer, fetch=daemon.search,
                         on_change=lambda: changes.append(1))
    model.apply_status(status)
    return model, daemon, runner, timers[0]


def test_short_queries_explain_and_never_search():
    model, daemon, runner, timer = _search()
    model.set_query("a")
    assert timer.delay is None and runner.queue == []
    assert model.view()["hint"] == search_view.hint({"query": "a"})
    assert daemon.queries == []


def test_search_waits_for_a_pause_and_groups_the_answer():
    model, daemon, runner, timer = _search()
    model.set_query("no")
    model.set_query("noon")
    assert timer.delay == search_view.TYPING_DELAY_MS
    assert model.view()["loading"] is True and model.view()["hint"] == ""
    timer.fire()
    runner.run_all()
    assert daemon.queries == [("noon", LIMIT)]
    view = model.view()
    assert [section["id"] for section in view["sections"]] == [
        "conversations", "messages", "contacts", "calls"]
    assert view["sections"][1]["rows"][0]["highlight"] == (11, 4)
    assert [model.target_at(index)["kind"] for index in range(4)] == [
        "conversation", "message", "contact", "call"]
    assert model.target_at(1) == {"kind": "message", "thread_key": "t1", "handle": "h9"}
    assert model.target_at(4) is None


def test_only_the_newest_answer_is_shown():
    model, _daemon, runner, timer = _search()
    model.set_query("ada")
    timer.fire()
    model.set_query("adam")
    timer.fire()
    first, second = runner.queue
    runner.queue = [second, first]
    runner.run_all()
    assert model.view()["sections"][0]["rows"][0]["title"] == "Ada"
    assert model.target_at(0) is not None
    assert json.loads(_answer("adam"))["query"] == "adam"


def test_a_full_search_queue_is_retried_a_few_times():
    model, daemon, runner, timer = _search()
    daemon.busy = 1
    model.set_query("noon")
    timer.fire()
    runner.run_all()
    assert timer.delay == search_view.TYPING_DELAY_MS, "tries again after a pause"
    assert model.view()["error"] == ""
    timer.fire()
    runner.run_all()
    assert model.view()["count"] == 4
    daemon.busy = BUSY_RETRIES + 1
    model.set_query("noon again")
    for _attempt in range(BUSY_RETRIES + 1):
        timer.fire()
        runner.run_all()
    assert model.view()["error"] == search_view.search_busy_text()
    assert model.view()["loading"] is False


def test_the_limit_note_counts_the_limit_the_model_asked_for():
    model, daemon, runner, timer = _search()
    daemon.cap = LIMIT - 5
    model.set_query("noon")
    timer.fire()
    runner.run_all()
    assert model.view()["limit_text"] == search_view.limit_text(
        json.loads(daemon.search("noon", LIMIT)), LIMIT)
    assert str(LIMIT - 5) in model.view()["limit_text"]


def test_errors_are_shown_and_clear_forgets_everything():
    model, daemon, runner, timer = _search()
    daemon.fail = "the running backend cannot search"
    model.set_query("noon")
    timer.fire()
    runner.run_all()
    assert model.view()["error"] == "the running backend cannot search"
    assert model.view()["sections"] == [] and model.target_at(0) is None
    daemon.fail = ""
    model.set_query("noon!")
    timer.fire()
    runner.run_all()
    assert model.view()["count"] == 4
    model.clear()
    assert model.query == "" and model.view()["sections"] == []
    assert model.target_at(0) is None


def test_an_unsupported_daemon_is_never_asked():
    model, daemon, runner, timer = _search(status={"daemon": True})
    model.set_query("noon")
    model.run()
    assert timer.delay is None and runner.queue == [] and daemon.queries == []
    assert model.view()["supported"] is False


def test_a_not_ready_error_name_kept_on_the_error_counts_as_busy():
    class NamedError(Exception):
        dbus_name = "io.weirdware.BlueFerry1.Error.NotReady"

    calls = []

    def fetch(query, limit):
        calls.append(query)
        if len(calls) == 1:
            raise NamedError("busy")
        return _answer(query)

    runner, timers = Runner(), []

    def timer(callback):
        timers.append(Timer(callback))
        return timers[-1]

    model = GlobalSearch(runner=runner, timer=timer, fetch=fetch)
    model.apply_status(STATUS)
    model.set_query("noon")
    timers[0].fire()
    runner.run_all()
    assert model.view()["error"] == "" and timers[0].delay == search_view.TYPING_DELAY_MS
    timers[0].fire()
    runner.run_all()
    assert model.view()["count"] == 4 and calls == ["noon", "noon"]
