"""Contact cards and the global search in the Textual client (fakes only)."""
from __future__ import annotations

import threading

from rich.text import Text
from textual.widgets import Button, Input

from blueferry import search_view
from blueferry.client import BackendError, ContactNotFoundError
from blueferry.models import CallHistoryEntry
from blueferry.protocol import ERROR_PREFIX
from blueferry.search_model import BUSY_RETRIES
from blueferry.tui import BlueFerryApp, HelpScreen, NewMessageScreen, TuiState
from blueferry.tui_calls import DialConfirmScreen
from blueferry.tui_contacts import ContactItem, ContactsScreen, DetailItem, MoreItem
from blueferry.tui_phone import CallGroupItem, RecentCallsScreen
from blueferry.tui_search import ResultItem, SearchScreen, result_text
from tests.test_tui import _run_headless
from tests.test_tui_phone import _CallingBackend, _until

HOSTILE = "[@click=app.quit]Ana[/]\x1b[31m"


def _card(index: int) -> dict:
    return {
        "id": f"c{index}", "name": f"Person {index:03d}", "organization": "",
        "primary": f"+4179000{index:04d}", "phone_count": 1, "email_count": 0,
        "address_count": 0, "has_photo": False,
    }


class _ContactsBackend(_CallingBackend):
    def __init__(self, **extra) -> None:
        extra.setdefault("contact_card_count", 3)
        extra.setdefault("contact_card_epoch", 1)
        extra.setdefault("search_result_limit", 50)
        super().__init__(**extra)
        self.cards = [
            {**_card(0), "name": HOSTILE, "organization": "ACME [b]x[/b]"},
            _card(1),
            _card(2),
        ]
        self.card_calls: list[tuple[int, int, str]] = []
        self.opened: list[str] = []
        self.stale: set[str] = set()
        self.searches: list[tuple[str, int]] = []
        self.search_reply: dict | Exception = {"query": "", "limit": 20}

    def contact_cards(self, offset: int = 0, limit: int = 150, query: str = "") -> dict:
        self.card_calls.append((offset, limit, query))
        cards = [
            card for card in self.cards
            if not query or query.casefold() in card["name"].casefold()
        ]
        return {
            "contacts": cards[offset:offset + limit], "total": len(cards),
            "offset": offset, "query": query,
        }

    def contact(self, contact_id: str) -> tuple[dict, bytes]:
        self.opened.append(contact_id)
        if contact_id in self.stale:
            raise ContactNotFoundError("no such card")
        return {
            "id": contact_id, "name": HOSTILE, "organization": "ACME", "title": "Boss",
            "phones": [
                {"kind": "mobile", "label": "", "value": "079 000 00 00",
                 "number": "+41790000000"},
                {"kind": "other_fax", "label": "", "value": "fax", "number": ""},
            ],
            "emails": [{"kind": "work", "label": "", "value": "ana@example.com"}],
            "addresses": [{
                "kind": "custom", "label": "Cabin [b]", "po_box": "", "extended": "",
                "street": "Main St 1", "locality": "Bern", "region": "",
                "postal_code": "3000", "country": "Switzerland",
            }],
            "has_photo": True, "photo_retry": False,
        }, b"\xff\xd8photo"

    def search(self, query: str, limit: int = 20) -> dict:
        self.searches.append((query, limit))
        if isinstance(self.search_reply, Exception):
            raise self.search_reply
        return {**self.search_reply, "query": query}


def _contacts_screen(app) -> ContactsScreen | None:
    screen = app.screen
    return screen if isinstance(screen, ContactsScreen) and screen.is_mounted else None


async def _open_contacts(app, pilot) -> ContactsScreen:
    await _until(pilot, lambda: "contact_card_count" in app.state.status.extra)
    app.set_focus(None)
    await pilot.press("C")
    await _until(pilot, lambda: _contacts_screen(app) is not None)
    screen = _contacts_screen(app)
    assert screen is not None
    await _until(pilot, lambda: bool(list(screen.query(ContactItem))))
    return screen


def test_contacts_list_search_and_detail_render_iphone_text_as_plain_text() -> None:
    backend = _ContactsBackend()

    async def scenario() -> None:
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(130, 44)) as pilot:
            screen = await _open_contacts(app, pilot)
            assert backend.card_calls[0] == (0, 100, "")
            body = screen.body_text
            assert "[@click=app.quit]Ana[/]�[31m  ACME [b]x[/b] · +41790000000" in body
            assert "Person 001" in body
            assert "3 contacts" in str(screen.query_one("#contacts-count").render())

            # Enter on a row opens the card: every number, mail and address.
            listing = screen.query_one("#contacts-list")
            listing.focus()
            listing.index = 0
            await pilot.press("enter")
            await _until(pilot, lambda: screen.card is not None)
            await _until(pilot, lambda: bool(list(screen.query(DetailItem))))
            detail = screen.detail_text
            assert "[@click=app.quit]Ana[/]�[31m" in detail
            assert "Boss · ACME" in detail
            assert "Mobile  079 000 00 00" in detail
            assert "Fax  fax" in detail
            assert "Work  ana@example.com" in detail
            assert "Cabin [b]  Main St 1" in detail and "3000 Bern" in detail
            assert "Switzerland" in detail
            for item in screen.query(DetailItem):
                assert isinstance(item.tooltip, Text)

            # Typing filters after the short delay; the old answer is replaced.
            search = screen.query_one("#contacts-search", Input)
            search.focus()
            await pilot.press(*"person 00")
            await _until(pilot, lambda: backend.card_calls[-1][2] == "person 00")
            await _until(pilot, lambda: "Ana" not in screen.body_text)
            assert "Person 001" in screen.body_text
            assert "2 matching contacts" in str(screen.query_one("#contacts-count").render())

            search.value = "nobody"
            await _until(pilot, lambda: backend.card_calls[-1][2] == "nobody")
            await _until(pilot, lambda: not screen.query_one("#contacts-list").display)
            assert "No contact matches “nobody”." in str(
                screen.query_one("#contacts-state").render()
            )

    _run_headless(scenario())


def test_contact_actions_call_message_and_copy_the_selected_row() -> None:
    backend = _ContactsBackend(calls_enabled=True, calls_state="ready")

    async def scenario() -> None:
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(130, 44)) as pilot:
            screen = await _open_contacts(app, pilot)
            screen.open_card("c0")
            await _until(pilot, lambda: bool(list(screen.query(DetailItem))))
            details = screen.query_one("#contacts-detail")
            details.focus()
            await _until(pilot, lambda: details.index == 1)
            await pilot.pause(0.05)
            assert not screen.query_one("#contacts-call", Button).disabled
            assert not screen.query_one("#contacts-message", Button).disabled

            # A number without a normalized form can only be copied.
            details.index = 2
            await pilot.pause(0.05)
            assert screen.query_one("#contacts-call", Button).disabled
            assert screen.query_one("#contacts-message", Button).disabled
            copied: list[str] = []
            app.copy_to_clipboard = copied.append  # type: ignore[method-assign]
            await pilot.press("y")
            assert copied == ["fax"]

            details.index = 1
            await pilot.press("c")
            await _until(pilot, lambda: isinstance(app.screen, DialConfirmScreen)
                         and bool(app.screen.query("#calls-confirm-call")))
            assert app.screen.number == "+41790000000"
            app.screen.dismiss(False)
            await pilot.pause(0.1)

            app.set_focus(None)
            app.action_contacts()
            screen = await _open_contacts(app, pilot)
            screen.open_card("c0")
            await _until(pilot, lambda: bool(list(screen.query(DetailItem))))
            details = screen.query_one("#contacts-detail")
            details.focus()
            details.index = 4  # the email row: message, copy
            await pilot.press("m")
            await _until(pilot, lambda: isinstance(app.screen, NewMessageScreen))
            await pilot.pause(0.1)
            assert app.screen.query_one("#new-recipient", Input).value == "ana@example.com"

    _run_headless(scenario())


def test_call_is_disabled_with_the_calls_hint_until_calls_are_ready() -> None:
    backend = _ContactsBackend()

    async def scenario() -> None:
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(130, 44)) as pilot:
            screen = await _open_contacts(app, pilot)
            screen.open_card("c0")
            await _until(pilot, lambda: bool(list(screen.query(DetailItem))))
            screen.query_one("#contacts-detail").focus()
            await pilot.pause(0.1)
            call = screen.query_one("#contacts-call", Button)
            assert call.disabled
            assert "Calls & Audio" in str(call.tooltip)
            screen.action_call()
            await pilot.pause(0.1)
            assert isinstance(app.screen, ContactsScreen)
            assert backend.dialed == []

    _run_headless(scenario())


def test_contacts_page_on_reaching_the_end() -> None:
    backend = _ContactsBackend()
    backend.cards = [_card(index) for index in range(130)]

    async def scenario() -> None:
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(130, 44)) as pilot:
            screen = await _open_contacts(app, pilot)
            await _until(pilot, lambda: bool(list(screen.query(MoreItem))))
            assert len(list(screen.query(ContactItem))) == 100
            listing = screen.query_one("#contacts-list")
            listing.focus()
            listing.index = 100  # the "Load more" row
            await _until(pilot, lambda: len(list(screen.query(ContactItem))) == 130)
            assert (100, 100, "") in backend.card_calls
            assert not list(screen.query(MoreItem))
            assert "Person 129" in screen.body_text

    _run_headless(scenario())


def test_a_stale_card_says_so_and_a_new_epoch_reloads_the_list() -> None:
    backend = _ContactsBackend()
    backend.stale.add("c1")

    async def scenario() -> None:
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(130, 44)) as pilot:
            screen = await _open_contacts(app, pilot)
            loads = len(backend.card_calls)
            screen.open_card("c1")
            await _until(pilot, lambda: "changed or was removed" in str(
                screen.query_one("#contacts-detail-state").render()
            ))
            await _until(pilot, lambda: len(backend.card_calls) == loads + 1)

            screen.open_card("c0")
            await _until(pilot, lambda: screen.card is not None)
            loads, reads = len(backend.card_calls), len(backend.opened)
            backend.extra["contact_card_epoch"] = 2
            app._load_data()
            # A new epoch reloads the list and reads the open card again.
            await _until(pilot, lambda: len(backend.card_calls) == loads + 1)
            await _until(pilot, lambda: len(backend.opened) == reads + 1)
            assert backend.opened[-1] == "c0" and screen.card is not None
            # The same status again does not read the list once more.
            app._load_data()
            await pilot.pause(0.3)
            assert len(backend.card_calls) == loads + 1
            assert len(backend.opened) == reads + 1

    _run_headless(scenario())


def test_contacts_need_a_daemon_that_offers_cards() -> None:
    backend = _ContactsBackend()
    del backend.extra["contact_card_count"]

    async def scenario() -> None:
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(130, 44)) as pilot:
            await pilot.pause(0.3)
            app.set_focus(None)
            await pilot.press("C")
            await pilot.pause(0.1)
            assert not isinstance(app.screen, ContactsScreen)
            assert backend.card_calls == []

    _run_headless(scenario())


def test_a_failed_page_shows_the_error_as_plain_text() -> None:
    backend = _ContactsBackend()

    def failing(offset: int = 0, limit: int = 150, query: str = "") -> dict:
        raise BackendError("[b]locked[/b]")

    backend.contact_cards = failing  # type: ignore[method-assign]

    async def scenario() -> None:
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(130, 44)) as pilot:
            await _until(pilot, lambda: "contact_card_count" in app.state.status.extra)
            app.set_focus(None)
            await pilot.press("C")
            await _until(pilot, lambda: _contacts_screen(app) is not None)
            screen = app.screen
            await _until(pilot, lambda: "[b]locked[/b]" in str(
                screen.query_one("#contacts-state").render()
            ))

    _run_headless(scenario())


# -- global search --------------------------------------------------------


_SEARCH_REPLY = {
    "limit": 20,
    "conversations": {"total": 1, "results": [
        {"thread_key": "one", "name": HOSTILE, "is_group": False,
         "timestamp": "2026-10-01T10:00:00+02:00"},
    ]},
    "messages": {"total": 25, "results": [
        {"thread_key": "one", "handle": "h1", "thread_name": "Alice", "is_group": False,
         "sender": "Alice", "outgoing": False, "timestamp": "2026-10-01T10:00:00+02:00",
         "snippet": "lunch at [b]noon[/b]?", "match_start": 0, "match_length": 5},
    ]},
    "contacts": {"total": 1, "results": [{**_card(0), "name": "Ana"}]},
    "calls": {"total": 1, "available": True, "results": [
        {"id": "call-2", "direction": "missed", "timestamp": "2026-10-01T09:00:00+02:00",
         "address": "+41790000001", "name": "", "contact_name": ""},
    ]},
}


def _search_screen(app) -> SearchScreen | None:
    screen = app.screen
    return screen if isinstance(screen, SearchScreen) and screen.is_mounted else None


async def _search(app, pilot, backend, text: str) -> SearchScreen:
    await _until(pilot, lambda: "search_result_limit" in app.state.status.extra)
    app.set_focus(None)
    await pilot.press("f")
    await _until(pilot, lambda: _search_screen(app) is not None)
    screen = _search_screen(app)
    assert screen is not None
    await pilot.press(*text)
    await _until(pilot, lambda: bool(list(screen.query(ResultItem))))
    return screen


def test_search_groups_hits_highlights_the_match_and_waits_for_typing() -> None:
    backend = _ContactsBackend()
    backend.search_reply = _SEARCH_REPLY

    async def scenario() -> None:
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(130, 44)) as pilot:
            screen = await _search(app, pilot, backend, "lunch")
            # One search after the typing delay, not one per key.
            assert backend.searches == [("lunch", 20)]
            body = screen.body_text
            lines = body.splitlines()
            assert lines[0] == "Conversations"
            assert "[@click=app.quit]Ana[/]�[31m" in lines[1]
            assert "Messages  (24 more matches)" in body
            assert "lunch at [b]noon[/b]?" in body
            assert "Contacts" in body and "Calls" in body
            assert "Missed call" in body
            assert "Showing at most 20 results per group." in str(
                screen.query_one("#search-limit").render()
            )
            message = next(
                item for item in screen.query(ResultItem) if item.section == "messages"
            )
            rendered = result_text(message.row)
            highlighted = [
                rendered.plain[span.start:span.end] for span in rendered.spans
                if "reverse" in str(span.style)
            ]
            assert highlighted == ["lunch"]

            # Too short: the hint, and no search.
            screen.query_one("#search-input", Input).value = "l"
            await _until(pilot, lambda: not list(screen.query(ResultItem)))
            assert "at least two characters" in str(screen.query_one("#search-state").render())
            assert len(backend.searches) == 1

    _run_headless(scenario())


def test_search_opens_the_conversation_of_a_message_hit() -> None:
    backend = _ContactsBackend()
    backend.search_reply = _SEARCH_REPLY

    async def scenario() -> None:
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(130, 44)) as pilot:
            screen = await _search(app, pilot, backend, "lunch")
            results = screen.query_one("#search-results")
            results.focus()
            results.index = 1  # the conversation hit
            await pilot.press("enter")
            await _until(pilot, lambda: not isinstance(app.screen, SearchScreen))
            await _until(pilot, lambda: app.state.selected_key == "one")

    _run_headless(scenario())


def test_search_opens_contact_cards_and_recent_calls_at_the_hit() -> None:
    backend = _ContactsBackend(call_history_enabled=True)
    backend.search_reply = _SEARCH_REPLY
    backend.history = [
        CallHistoryEntry("outgoing", "2026-10-01T11:00:00+02:00", "+41790000009",
                         extra={"id": "call-1"}),
        CallHistoryEntry("missed", "2026-10-01T09:00:00+02:00", "+41790000001",
                         extra={"id": "call-2"}),
    ]

    async def scenario() -> None:
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(130, 44)) as pilot:
            screen = await _search(app, pilot, backend, "ana")
            items = list(screen.query(ResultItem))
            contact = next(item for item in items if item.section == "contacts")
            results = screen.query_one("#search-results")
            results.index = results.children.index(contact)
            screen.open_selected()
            await _until(pilot, lambda: _contacts_screen(app) is not None)
            await _until(pilot, lambda: backend.opened == ["c0"])
            app.screen.action_close()
            await _until(pilot, lambda: not isinstance(app.screen, ContactsScreen))

            screen = await _search(app, pilot, backend, "079")
            call = next(item for item in screen.query(ResultItem) if item.section == "calls")
            results = screen.query_one("#search-results")
            results.index = results.children.index(call)
            screen.open_selected()
            await _until(pilot, lambda: isinstance(app.screen, RecentCallsScreen))
            recent = app.screen
            await _until(pilot, lambda: bool(list(recent.query(CallGroupItem))))
            await pilot.pause(0.1)
            highlighted = recent.query_one("#recent-calls-list").highlighted_child
            assert isinstance(highlighted, CallGroupItem)
            assert highlighted.row["address"] == "+41790000001"

    _run_headless(scenario())


def test_a_call_hit_selects_the_row_that_folds_that_call() -> None:
    """Two missed calls of one number on one day, apart: the hit's own row."""
    backend = _ContactsBackend(call_history_enabled=True)
    backend.history = [
        CallHistoryEntry("missed", "2026-10-01T11:00:00+02:00", "+41790000001",
                         extra={"id": "call-1"}),
        CallHistoryEntry("outgoing", "2026-10-01T10:00:00+02:00", "+41790000009",
                         extra={"id": "call-2"}),
        CallHistoryEntry("missed", "2026-10-01T09:00:00+02:00", "+41790000001",
                         extra={"id": "call-3"}),
    ]

    async def scenario() -> None:
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(130, 44)) as pilot:
            await pilot.pause(0.2)
            app._search_hit_chosen({"kind": "call", "call_id": "call-3"})
            await _until(pilot, lambda: isinstance(app.screen, RecentCallsScreen))
            recent = app.screen
            await _until(pilot, lambda: len(list(recent.query(CallGroupItem))) == 3)
            await pilot.pause(0.1)
            highlighted = recent.query_one("#recent-calls-list").highlighted_child
            assert isinstance(highlighted, CallGroupItem)
            assert highlighted.row["ids"] == ["call-3"]

    _run_headless(scenario())


def test_search_retries_when_the_daemon_is_busy_then_says_so() -> None:
    backend = _ContactsBackend()
    backend.search_reply = BackendError(
        "[b]busy[/b]", dbus_name=f"{ERROR_PREFIX}.NotReady",
    )

    async def scenario() -> None:
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(130, 44)) as pilot:
            await _until(pilot, lambda: "search_result_limit" in app.state.status.extra)
            app.set_focus(None)
            await pilot.press("f")
            await _until(pilot, lambda: _search_screen(app) is not None)
            screen = app.screen
            await pilot.press(*"ana")
            await _until(pilot, lambda: search_view.search_busy_text() in str(
                screen.query_one("#search-state").render()
            ))
            # The shared model: the first try and BUSY_RETRIES more.
            assert backend.searches == [("ana", 20)] * (BUSY_RETRIES + 1)

            backend.search_reply = BackendError("down")
            screen.query_one("#search-input", Input).value = "anna"
            await _until(pilot, lambda: "down" in str(screen.query_one("#search-state").render()))
            assert backend.searches[-1] == ("anna", 20)
            assert len(backend.searches) == BUSY_RETRIES + 2  # others are not retried

    _run_headless(scenario())


def test_a_superseded_search_answer_is_dropped() -> None:
    backend = _ContactsBackend()
    release = threading.Event()
    answered: list[str] = []
    original = backend.search

    started = threading.Event()

    def slow(query: str, limit: int = 20) -> dict:
        if query == "first":
            started.set()
            release.wait(5)
        answered.append(query)
        return {**original(query, limit), **(_SEARCH_REPLY if query == "first" else {})}

    backend.search = slow  # type: ignore[method-assign]

    async def scenario() -> None:
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(130, 44)) as pilot:
            await _until(pilot, lambda: "search_result_limit" in app.state.status.extra)
            app.set_focus(None)
            await pilot.press("f")
            await _until(pilot, lambda: _search_screen(app) is not None)
            screen = app.screen
            search = screen.query_one("#search-input", Input)
            search.value = "first"
            await _until(pilot, started.is_set)
            search.value = "second"
            await _until(pilot, lambda: "second" in answered)
            release.set()
            await _until(pilot, lambda: "first" in answered)
            await pilot.pause(0.2)
            assert not list(screen.query(ResultItem))
            assert "Nothing matches “second”." in str(screen.query_one("#search-state").render())

    _run_headless(scenario())


def test_search_needs_a_daemon_that_offers_it_and_help_lists_the_keys() -> None:
    backend = _ContactsBackend()
    del backend.extra["search_result_limit"]

    async def scenario() -> None:
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(130, 44)) as pilot:
            await pilot.pause(0.3)
            app.set_focus(None)
            await pilot.press("f")
            await pilot.pause(0.1)
            assert not isinstance(app.screen, SearchScreen)
            assert backend.searches == []

            app.action_help()
            await _until(pilot, lambda: isinstance(app.screen, HelpScreen))
            await pilot.pause(0.1)
            text = "\n".join(
                str(static.render()) for static in app.screen.query(".dialog-copy")
            )
            assert "Search messages, contacts and calls  f" in text
            assert "Contacts · call · message · copy  C · c m y" in text

    _run_headless(scenario())


def test_the_command_palette_lists_contacts_and_search() -> None:
    backend = _ContactsBackend()

    async def scenario() -> None:
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(130, 44)) as pilot:
            await pilot.pause(0.2)
            titles = [command.title for command in app.get_system_commands(app.screen)]
            assert "Contacts" in titles
            assert "Search Everything" in titles

    _run_headless(scenario())
