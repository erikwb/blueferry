"""Global search: in-memory matching, the Search operation, CLI and presenter."""
from __future__ import annotations

import json
import unicodedata
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from blueferry import cli_search, config, search_view
from blueferry.backend_operations import BackendDependencies, BackendOperations
from blueferry.call_history import CallRecord, call_entry_id
from blueferry.cli import app
from blueferry.contact_repository import ContactRepository
from blueferry.contacts import ContactsResolver
from blueferry.errors import InvalidArgumentsError, NotReadyError
from blueferry.global_search import ThreadSearchIndex, search_calls, search_threads, snippet
from blueferry.history import append_event, clear_events
from blueferry.limits import MAX_SEARCH_RESULTS, MAX_SEARCH_SNIPPET_CHARS
from blueferry.models import CallHistoryEntry

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


def _call(direction, minutes_ago, number="15551230001", name=None) -> CallRecord:
    moment = NOW - timedelta(minutes=minutes_ago)
    return CallRecord(
        direction=direction, occurred_at=moment,
        raw_time=moment.strftime("%Y%m%dT%H%M%SZ"),
        address=f"+{number}", phone=number, name=name,
    )


THREADS = [
    {"key": "address:15551112222", "name": "Alice Example", "is_group": False,
     "members": [], "last_ts": "2026-10-30T11:00:00Z", "messages": [
         {"handle": "m1", "body": "Lunch at the\nusual place?", "sender": "",
          "timestamp": "2026-10-30T10:00:00Z", "outgoing": False},
         {"handle": "m2", "body": "Sure, lunch at noon", "sender": "",
          "timestamp": "2026-10-30T11:00:00+01:00", "outgoing": True},
     ]},
    {"key": "group:named:x", "name": "Climbing", "is_group": True,
     "members": ["Bob Lunchbox"], "last_ts": "2026-10-29T09:00:00Z", "messages": [
         {"handle": "g1", "body": "no match here", "sender": "Bob",
          "timestamp": "2026-10-29T09:00:00Z", "outgoing": False},
     ]},
]


# ---- matching -------------------------------------------------------------------

def test_threads_match_names_members_and_bodies_newest_first() -> None:
    conversations, messages = search_threads(THREADS, "lunch", 10)
    assert conversations["total"] == 1
    assert conversations["results"] == [{
        "thread_key": "group:named:x", "name": "Climbing", "is_group": True,
        "timestamp": "2026-10-29T09:00:00Z",
    }]
    assert messages["total"] == 2
    # 11:00+01:00 is 10:00Z, the same instant as m1; m1 was stored first.
    assert [hit["handle"] for hit in messages["results"]] == ["m1", "m2"]
    first = messages["results"][0]
    assert first["thread_key"] == "address:15551112222"
    assert first["snippet"] == "Lunch at the usual place?"
    assert (first["match_start"], first["match_length"]) == (0, 5)
    # Whitespace in the query and the body is collapsed before matching.
    assert search_threads(THREADS, "the   usual", 10)[1]["total"] == 1


def test_highlight_follows_the_original_after_sharp_s() -> None:
    threads = [{"key": "t", "name": "T", "messages": [
        {"handle": "m", "body": "Straße Bern lunch", "timestamp": "2026-10-30T10:00:00Z"},
    ]}]
    for query in ("lunch", "LUNCH"):
        [hit] = search_threads(threads, query, 5)[1]["results"]
        start, length = hit["match_start"], hit["match_length"]
        assert hit["snippet"][start:start + length] == "lunch", query
    [hit] = search_threads(threads, "strasse", 5)[1]["results"]
    assert hit["snippet"][hit["match_start"]:][:hit["match_length"]] == "Straße"


@pytest.mark.parametrize("form", ["NFC", "NFD"])
@pytest.mark.parametrize("query", ["Müller", "Muller", "MULLER"])
def test_names_and_bodies_ignore_accents_and_unicode_forms(form, query) -> None:
    name = unicodedata.normalize(form, "Jörg Müller")
    threads = [{"key": "t", "name": name, "members": [], "messages": [
        {"handle": "m", "body": f"Grüsse von {name}!", "timestamp": "2026-10-30T10:00:00Z"},
    ]}]
    for spelling in (query, unicodedata.normalize("NFD", query)):
        conversations, messages = search_threads(threads, spelling, 5)
        assert conversations["total"] == 1, spelling
        [hit] = messages["results"]
        marked = hit["snippet"][hit["match_start"]:hit["match_start"] + hit["match_length"]]
        assert marked == unicodedata.normalize(form, "Müller"), spelling


def test_conversations_match_number_digits() -> None:
    threads = [{"key": "address:41791234567", "name": "+41 79 123 45 67", "members": [],
                "recipients": ["+41791234567"], "messages": []},
               {"key": "address:15550000000", "name": "Alice", "members": [],
                "recipients": ["+15550000000"], "messages": []}]
    assert search_threads(threads, "79 123", 5)[0]["results"][0]["thread_key"] == (
        "address:41791234567"
    )
    assert search_threads(threads, "555 000", 5)[0]["results"][0]["name"] == "Alice"
    assert search_threads(threads, "1 4", 5)[0]["total"] == 0  # too short for digits


def test_thread_index_is_reused_for_one_projection(operations, monkeypatch) -> None:
    operations.search("lunch", 10)
    index = operations._search_index
    assert isinstance(index, ThreadSearchIndex)
    monkeypatch.setattr(
        "blueferry.backend_operations.ThreadSearchIndex",
        lambda _threads: pytest.fail("bodies were folded again"),
    )
    assert operations.search("report", 10)["messages"]["total"] == 1
    assert operations._search_index is index


def test_search_async_matches_off_the_main_loop(operations) -> None:
    jobs = []
    replies = []

    def submit(job, *, on_success, on_error):
        jobs.append((job, on_success, on_error))

    operations.search_async("alice", 10, submit, replies.append, pytest.fail)
    assert replies == [] and len(jobs) == 1
    job, on_success, _on_error = jobs[0]
    on_success(job())
    assert replies[0]["contacts"]["total"] == 1
    # The card query text folded by the job is kept by the live cache.
    assert operations.dependencies.contacts._card_haystacks is not None

    def busy(_job, **_callbacks):
        raise RuntimeError("busy")

    with pytest.raises(NotReadyError):
        operations.search_async("alice", 10, busy, replies.append, pytest.fail)


def test_thread_results_are_limited_but_counted() -> None:
    _conversations, messages = search_threads(THREADS, "lunch", 1)
    assert messages["total"] == 2 and len(messages["results"]) == 1


def test_snippet_keeps_context_and_marks_the_match() -> None:
    text = "x" * 500 + "needle" + "y" * 500
    excerpt, start, length = snippet(text, 500, 6)
    assert excerpt.startswith("…") and excerpt.endswith("…")
    assert len(excerpt) <= MAX_SEARCH_SNIPPET_CHARS + 2
    assert excerpt[start:start + length] == "needle"
    assert snippet("short", 0, 5) == ("short", 0, 5)


def test_calls_match_names_and_number_digits() -> None:
    records = [
        _call("missed", 1, "15551112222"),
        _call("outgoing", 2, "41791234567", name="Pizza Place"),
    ]
    def resolve(address):
        return "Alice Example" if address and address.endswith("5551112222") else None

    hits = search_calls(records, "alice", 10, resolve)
    assert hits["total"] == 1 and hits["results"][0]["name"] == "Alice Example"
    assert hits["results"][0]["id"] == call_entry_id(records[0])
    assert "5551112222" not in hits["results"][0]["id"]
    assert search_calls(records, "079 123 45", 10, None)["total"] == 0
    assert search_calls(records, "79 123 45", 10, None)["results"][0]["name"] == "Pizza Place"
    assert search_calls(records, "12", 10, None)["total"] == 0  # too short for digits


# ---- operation ------------------------------------------------------------------

class _Sessions:
    map = object()
    pbap = object()
    map_path = "/session/map"

    @staticmethod
    def report_error(_error):
        pass


@pytest.fixture
def operations(isolated_state):
    ContactRepository().replace([
        ("Alice Example", ["15551112222"], []), ("Bob Other", ["15553334444"], []),
    ])
    contacts = ContactsResolver(photos=lambda: False)
    for index, body in enumerate(("lunch tomorrow?", "the report is ready")):
        append_event({
            "kind": "sms_received", "handle": f"h{index}", "sender_address": "+15551112222",
            "body": body, "is_read": True, "seen_at": f"2026-10-30T1{index}:00:00Z",
        })
    history = SimpleNamespace(records=lambda: [_call("missed", 5, "15551112222")])
    return BackendOperations(_Sessions(), BackendDependencies(
        contacts=contacts, call_history=lambda: history,
    ))


def test_search_groups_every_source(operations) -> None:
    result = operations.search("alice", 10)
    assert result["query"] == "alice" and result["limit"] == 10
    assert result["conversations"]["results"][0]["name"] == "Alice Example"
    assert result["messages"]["total"] == 0
    [contact] = result["contacts"]["results"]
    assert contact["name"] == "Alice Example" and contact["id"]
    assert result["calls"]["results"][0]["name"] == "Alice Example"
    assert all(result[group]["available"] for group in search_view.GROUPS)
    report = operations.search("  REPORT ", 10)["messages"]["results"]
    assert [(hit["handle"], hit["thread_key"]) for hit in report] == [
        ("h1", operations.list_threads(5)[0]["key"]),
    ]
    # The call-history list reports the same id as the search hit.
    [listed] = operations.list_call_history(5)
    assert listed["id"] == result["calls"]["results"][0]["id"]
    assert CallHistoryEntry.from_dict(listed).call_id == listed["id"]


def test_search_bounds_and_short_queries(operations) -> None:
    assert operations.search("a", 10)["messages"] == {
        "total": 0, "results": [], "available": True,
    }
    assert operations.search("e", 10)["contacts"]["results"] == []
    assert operations.search("e x", 1000)["limit"] == MAX_SEARCH_RESULTS
    with pytest.raises(InvalidArgumentsError):
        operations.search("x" * 300, 10)
    assert operations.status()["search_result_limit"] == MAX_SEARCH_RESULTS


def test_search_without_call_history_or_contacts(isolated_state) -> None:
    operations = BackendOperations(
        _Sessions(), BackendDependencies(call_history=lambda: None),
    )
    result = operations.search("anything", 10)
    assert result["calls"]["available"] is False
    assert result["contacts"]["available"] is False
    assert result["messages"]["available"] is True


def test_search_skips_calls_while_storage_is_locked(isolated_state) -> None:
    locked = SimpleNamespace(status=SimpleNamespace(can_read=False), revision=0)
    history = SimpleNamespace(records=lambda: pytest.fail("locked history was read"))
    operations = BackendOperations(_Sessions(), BackendDependencies(
        call_history=lambda: history, storage=locked,
    ))
    operations._conversations.threads = lambda: []
    assert operations.search("anything", 10)["calls"]["available"] is False


class _Storage:
    """Readable until locked; decrypts the plain test history as-is."""

    def __init__(self) -> None:
        self.status = SimpleNamespace(can_read=True)
        self.revision = 0

    def lock(self) -> None:
        self.status = SimpleNamespace(can_read=False)
        self.revision += 1

    @staticmethod
    def decrypt(payload, *, purpose):
        return payload


def _queued(isolated_state, *, storage=None):
    """Operations over the fixture's data with one search queued, not delivered."""
    ContactRepository().replace([("Alice Example", ["15551112222"], [])])
    append_event({
        "kind": "sms_received", "handle": "h0", "sender_address": "+15551112222",
        "body": "lunch with alice", "is_read": True, "seen_at": "2026-10-30T10:00:00Z",
    })
    contacts = ContactsResolver(photos=lambda: False)
    calls = {"history": SimpleNamespace(records=lambda: [_call("missed", 5, "15551112222")])}
    operations = BackendOperations(_Sessions(), BackendDependencies(
        contacts=contacts, call_history=lambda: calls["history"], storage=storage,
    ))
    operations.list_threads(5)
    jobs, replies = [], []
    operations.search_async(
        "alice", 10, lambda job, **callbacks: jobs.append((job, callbacks)),
        replies.append, pytest.fail,
    )
    [(job, callbacks)] = jobs
    outcome = job()

    def deliver():
        callbacks["on_success"](outcome)
        return replies[-1]

    return operations, calls, contacts, deliver


def _found(result):
    return {group: result[group]["total"] for group in search_view.GROUPS}


def test_a_queued_search_keeps_results_when_messages_only_arrive(isolated_state) -> None:
    _operations, _calls, _contacts, deliver = _queued(isolated_state)
    append_event({
        "kind": "sms_received", "handle": "h1", "sender_address": "+15559990000",
        "body": "new", "is_read": True, "seen_at": "2026-10-30T11:00:00Z",
    })
    assert _found(deliver()) == {
        "conversations": 1, "messages": 1, "contacts": 1, "calls": 1,
    }


def test_a_queued_search_drops_messages_cleared_meanwhile(isolated_state) -> None:
    _operations, _calls, _contacts, deliver = _queued(isolated_state)
    clear_events()
    result = deliver()
    assert result["conversations"]["results"] == result["messages"]["results"] == []
    assert result["messages"]["total"] == 0 and result["calls"]["total"] == 1


def test_a_queued_search_drops_calls_turned_off_meanwhile(isolated_state) -> None:
    _operations, calls, _contacts, deliver = _queued(isolated_state)
    calls["history"] = None
    result = deliver()
    assert result["calls"] == {"total": 0, "results": [], "available": False}
    assert result["messages"]["total"] == 1


def test_a_queued_search_drops_calls_cleared_meanwhile(isolated_state) -> None:
    _operations, calls, _contacts, deliver = _queued(isolated_state)
    calls["history"].records = lambda: []
    assert deliver()["calls"] == {"total": 0, "results": [], "available": True}


def test_a_queued_search_drops_contacts_reloaded_meanwhile(isolated_state) -> None:
    _operations, _calls, contacts, deliver = _queued(isolated_state)
    ContactRepository().replace([])
    contacts.refresh()
    result = deliver()
    assert result["contacts"] == {"total": 0, "results": [], "available": True}
    # Call names came from the same contacts.
    assert result["calls"] == {"total": 0, "results": [], "available": True}


def test_a_queued_search_returns_nothing_once_storage_locks(isolated_state) -> None:
    storage = _Storage()
    _operations, _calls, _contacts, deliver = _queued(isolated_state, storage=storage)
    storage.lock()
    result = deliver()
    assert _found(result) == {
        "conversations": 0, "messages": 0, "contacts": 0, "calls": 0,
    }
    assert result["calls"]["available"] is False


def test_invalidating_conversations_forgets_the_folded_bodies(operations) -> None:
    operations.search("lunch", 10)
    assert operations._search_index is not None
    operations.invalidate_conversations()
    assert operations._search_index is None


def test_search_writes_nothing_to_disk(operations) -> None:
    operations.list_threads(5)

    def files():
        return {
            path: (path.stat().st_size, path.stat().st_mtime_ns)
            for root in (config.STATE_DIR, config.CONFIG_DIR) if root.exists()
            for path in root.rglob("*") if path.is_file()
        }

    before = files()
    operations.search("lunch", 10)
    operations.search("alice", 10)
    assert files() == before


# ---- presenter --------------------------------------------------------------------

RESULT = {
    "query": "lunch", "limit": 2,
    "conversations": {"total": 1, "available": True, "results": [
        {"thread_key": "g", "name": "Climbing", "is_group": True,
         "timestamp": "2026-10-29T09:00:00Z"},
    ]},
    "messages": {"total": 3, "available": True, "results": [
        {"thread_key": "t", "handle": "m1", "thread_name": "Alice", "is_group": False,
         "sender": "", "outgoing": True, "timestamp": "2026-10-30T10:00:00Z",
         "snippet": "Sure, lunch at noon", "match_start": 6, "match_length": 5},
        {"thread_key": "t", "handle": "", "snippet": "dropped"},
    ]},
    "contacts": {"total": 1, "available": True, "results": [
        {"id": "c1", "name": "Lunch Club", "organization": "", "primary": "+1 555"},
    ]},
    "calls": {"total": 0, "available": False, "results": []},
}


def test_presenter_sections_rows_and_targets() -> None:
    groups = search_view.sections(json.dumps(RESULT))
    assert [section["id"] for section in groups] == ["conversations", "messages", "contacts"]
    messages = groups[1]
    assert messages["more"] == "2 more matches" and len(messages["rows"]) == 1
    row = messages["rows"][0]
    assert row["title"] == "Alice" and row["subtitle"] == "You"
    assert row["snippet"][slice(row["highlight"][0], sum(row["highlight"]))] == "lunch"
    assert row["target"] == {"kind": "message", "thread_key": "t", "handle": "m1"}
    assert groups[0]["rows"][0]["subtitle"] == "Group conversation"
    assert groups[2]["rows"][0]["target"] == {"kind": "contact", "contact_id": "c1"}
    assert search_view.target_at(RESULT, 2) == {"kind": "contact", "contact_id": "c1"}
    assert search_view.target_at(RESULT, 3) is None and search_view.target_at(RESULT, True) is None
    assert "Call history is off" in search_view.hint(RESULT)
    assert search_view.search_view(RESULT)["count"] == 3


def test_presenter_hints_and_capability() -> None:
    assert "two characters" in search_view.hint({"query": "a"})
    assert "Nothing matches “zzz”" in search_view.hint({"query": "zzz"})
    assert search_view.sections("garbage") == []
    assert search_view.should_search(" ab ") and not search_view.should_search(" a ")
    assert search_view.supported({"search_result_limit": 50})
    assert not search_view.supported({})
    call = {"calls": {"results": [{"id": "k", "direction": "missed", "address": "+1555",
                                   "name": "Alice", "timestamp": "2026-10-30T10:00:00+00:00"}]}}
    [section] = search_view.sections(call)
    assert section["rows"][0]["subtitle"] == "Missed call · +1555"
    assert section["rows"][0]["target"] == {"kind": "call", "call_id": "k"}


# ---- CLI ---------------------------------------------------------------------------

def test_cli_prints_groups_snippets_and_ids(monkeypatch) -> None:
    hostile = {**RESULT, "contacts": {"total": 1, "results": [
        {"id": "c1", "name": "Lunch\x1b[31m Club"},
    ]}}
    monkeypatch.setattr(cli_search, "BackendClient", lambda: SimpleNamespace(
        search=lambda query, limit: hostile,
    ))
    result = CliRunner().invoke(app, ["search", "lunch", "--ids"])
    assert result.exit_code == 0, result.output
    assert "Messages (3)" in result.output and "Sure, lunch at noon" in result.output
    assert "thread_key=t handle=m1" in result.output and "contact_id=c1" in result.output
    assert "2 more matches; raise --limit" in result.output
    assert "\x1b[31m" not in result.output


def test_cli_exits_one_without_results(monkeypatch) -> None:
    monkeypatch.setattr(cli_search, "BackendClient", lambda: SimpleNamespace(
        search=lambda query, limit: {"query": query},
    ))
    result = CliRunner().invoke(app, ["search", "zzz"])
    assert result.exit_code == 1 and "Nothing matches" in result.output


def test_presenter_reports_the_applied_limit() -> None:
    assert search_view.search_view(RESULT)["limit"] == 2
    assert search_view.limit_text(RESULT) == "Showing at most 2 results per group."
    complete = {"query": "x", "limit": 50, "messages": {"total": 1, "results": [
        {"thread_key": "t", "handle": "m", "snippet": "x"},
    ]}}
    assert search_view.limit_text(complete) == ""
    assert search_view.limit_text(complete, 100) == "Showing at most 50 results per group."
    assert search_view.limit_text({"query": "x"}, 100) == ""


def test_cli_caps_the_limit_and_says_so(monkeypatch) -> None:
    requested = []

    def search(query, limit):
        requested.append(limit)
        return {**RESULT, "limit": limit}

    monkeypatch.setattr(cli_search, "BackendClient", lambda: SimpleNamespace(search=search))
    result = CliRunner().invoke(app, ["search", "lunch", "-n", "100"])
    assert result.exit_code == 0, result.output
    assert requested == [MAX_SEARCH_RESULTS]
    assert f"Showing at most {MAX_SEARCH_RESULTS} results per group." in result.output
    assert "refine the query" in result.output


def test_a_query_that_folds_away_matches_nothing() -> None:
    marks = "\u0301\u0308"
    conversations, messages = search_threads(THREADS, marks, 5)
    assert conversations["total"] == 0 and messages["total"] == 0
    assert search_calls([_call("missed", 1, name="Alice")], marks, 5, None)["total"] == 0
