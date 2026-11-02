"""The shared Contacts tab state (blueferry.contacts_model), without a toolkit."""
from __future__ import annotations

import json

from blueferry import contacts_view
from blueferry.client import ContactNotFoundError
from blueferry.contacts_model import PAGE_SIZE, ContactBook
from blueferry.search_view import TYPING_DELAY_MS

STATUS = {"contact_card_count": 3, "contact_card_epoch": 7}


class Runner:
    """Queues work; the test answers it on the 'UI thread' when it likes."""

    def __init__(self) -> None:
        self.queue: list = []

    def submit(self, operation, on_done=None, on_error=None, on_finished=None):
        self.queue.append((operation, on_done, on_error))

    def run_all(self) -> None:
        while self.queue:
            operation, on_done, on_error = self.queue.pop(0)
            try:
                value = operation()
            except Exception as error:  # mirrors the TaskRunner contract
                if on_error is not None:
                    on_error(str(error))
                continue
            if on_done is not None:
                on_done(value)


class Timer:
    def __init__(self, callback) -> None:
        self.callback = callback
        self.delay: int | None = None

    def start(self, delay_ms: int) -> None:
        self.delay = delay_ms

    def stop(self) -> None:
        self.delay = None

    def fire(self) -> None:
        self.delay = None
        self.callback()


def _card(card_id, name, **extra):
    return {"id": card_id, "name": name, "organization": "", "primary": "",
            "phone_count": 1, "email_count": 0, "address_count": 0,
            "has_photo": False, **extra}


class Daemon:
    def __init__(self, cards=None) -> None:
        self.cards = cards if cards is not None else [
            _card("c1", "Ada Lovelace", primary="+15550001"),
            _card("c2", "Bob"),
            _card("c3", "Chloé"),
        ]
        self.pages: list = []
        self.opened: list = []
        self.stale: set = set()
        self.fail = ""

    def page(self, offset, limit, query):
        self.pages.append((offset, limit, query))
        if self.fail:
            raise RuntimeError(self.fail)
        matches = [c for c in self.cards if query.casefold() in c["name"].casefold()]
        return json.dumps({"contacts": matches[offset:offset + limit],
                           "total": len(matches), "offset": offset, "query": query})

    def card(self, card_id):
        self.opened.append(card_id)
        if card_id in self.stale:
            raise ContactNotFoundError("unknown contact")
        return json.dumps({
            "id": card_id, "name": "Ada Lovelace", "organization": "Analytical",
            "title": "Countess",
            "phones": [{"kind": "mobile", "label": "", "value": "+1 555 0001",
                        "number": "+15550001"}],
            "emails": [{"kind": "work", "label": "", "value": "ada@example.org"}],
            "addresses": [], "has_photo": True,
        }), b"\x89PNG"


def _book(daemon=None, *, status=STATUS):
    daemon = daemon or Daemon()
    runner = Runner()
    timers: list[Timer] = []
    changes: list[int] = []

    def timer(callback):
        timers.append(Timer(callback))
        return timers[-1]

    book = ContactBook(runner=runner, timer=timer, fetch_page=daemon.page,
                       fetch_card=daemon.card, on_change=lambda: changes.append(1))
    if status is not None:
        book.apply_status(status)
    return book, daemon, runner, timers[0], changes


def test_nothing_is_read_until_the_tab_is_shown_and_hiding_forgets_it():
    book, daemon, runner, _timer, _changes = _book()
    runner.run_all()
    assert daemon.pages == [] and book.rows == []
    book.watch(True)
    runner.run_all()
    assert daemon.pages == [(0, PAGE_SIZE, "")]
    view = book.view()
    assert [row["title"] for row in view["rows"]] == ["Ada Lovelace", "Bob", "Chloé"]
    assert view["count_text"] == contacts_view.count_text(daemon.page(0, PAGE_SIZE, ""))
    book.open("c1")
    runner.run_all()
    assert book.card["open"] and book.photo == b"\x89PNG"
    book.watch(False)
    assert book.rows == [] and not book.card["open"] and book.photo == b""
    assert book.view()["count_text"] == ""


def test_an_unsupported_daemon_is_never_asked():
    book, daemon, runner, _timer, _changes = _book(status={"daemon": True})
    book.watch(True)
    runner.run_all()
    assert daemon.pages == [] and book.view()["supported"] is False


def test_typing_waits_for_a_pause_and_drops_superseded_pages():
    book, daemon, runner, timer, _changes = _book()
    book.watch(True)
    runner.run_all()
    book.set_query(" ad ")
    assert timer.delay == TYPING_DELAY_MS
    assert book.query == "ad"
    book.set_query("ada")
    timer.fire()
    # A slower answer for an older reload must not replace the newer one.
    book.reload()
    first, second = runner.queue
    runner.queue = [second, first]
    runner.run_all()
    assert daemon.pages[-1] == (0, PAGE_SIZE, "ada")
    assert [row["id"] for row in book.rows] == ["c1"]
    assert book.view()["count_text"] == "1 matching contact"
    book.set_query("zzz")
    timer.fire()
    runner.run_all()
    assert book.view()["hint"] == contacts_view.empty_hint(
        {"contacts": [], "total": 0, "query": "zzz"})


def test_more_pages_load_on_demand_without_duplicates():
    cards = [_card(f"c{index:03d}", f"Person {index:03d}") for index in range(PAGE_SIZE + 5)]
    book, daemon, runner, _timer, _changes = _book(Daemon(cards))
    book.watch(True)
    runner.run_all()
    assert len(book.rows) == PAGE_SIZE and book.view()["has_more"] is True
    book.load_more()
    book.load_more()  # one page at a time
    runner.run_all()
    assert daemon.pages[-1] == (PAGE_SIZE, PAGE_SIZE, "")
    assert len(daemon.pages) == 2
    assert len(book.rows) == PAGE_SIZE + 5 and book.view()["has_more"] is False
    assert len({row["id"] for row in book.rows}) == PAGE_SIZE + 5


def test_card_shows_every_detail_from_the_presenter():
    book, _daemon, runner, _timer, _changes = _book()
    book.watch(True)
    runner.run_all()
    book.open("c1")
    assert book.card["loading"] is True
    runner.run_all()
    card = book.card
    assert card["title"] == "Ada Lovelace"
    assert card["subtitle"] == "Countess · Analytical"
    assert [section["id"] for section in card["sections"]] == ["phones", "emails"]
    assert card["sections"][0]["rows"][0]["target"] == "+15550001"
    book.close()
    assert not book.card["open"] and book.photo == b""


def test_a_stale_card_says_so_and_reloads_the_list():
    book, daemon, runner, _timer, _changes = _book()
    book.watch(True)
    runner.run_all()
    daemon.stale.add("c2")
    book.open("c2")
    runner.run_all()
    assert book.card["stale"] is True
    assert book.card["error"] == contacts_view.stale_text()
    assert daemon.pages.count((0, PAGE_SIZE, "")) == 2


def test_a_new_epoch_reloads_the_list_and_rereads_the_open_card():
    book, daemon, runner, _timer, _changes = _book()
    book.watch(True)
    runner.run_all()
    book.open("c1")
    runner.run_all()
    book.apply_status(STATUS)  # unchanged epoch: nothing happens
    assert runner.queue == []
    book.apply_status({**STATUS, "contact_card_epoch": 8})
    runner.run_all()
    assert len(daemon.pages) == 2 and daemon.opened == ["c1", "c1"]
    assert book.card["title"] == "Ada Lovelace"


def test_failures_are_shown_and_cleared_by_the_next_answer():
    book, daemon, runner, _timer, _changes = _book()
    daemon.fail = "the running backend cannot show contact cards"
    book.watch(True)
    runner.run_all()
    assert book.view()["error"] == "the running backend cannot show contact cards"
    assert book.view()["hint"] == ""
    daemon.fail = ""
    book.reload()
    runner.run_all()
    assert book.view()["error"] == "" and len(book.rows) == 3


def test_answers_after_hiding_are_dropped():
    book, _daemon, runner, _timer, _changes = _book()
    book.watch(True)
    book.watch(False)
    runner.run_all()
    assert book.rows == [] and book.view()["loaded"] is False


# ---- row photos ---------------------------------------------------------

class PhotoDaemon(Daemon):
    def __init__(self) -> None:
        super().__init__([
            _card("c1", "Ada", has_photo=True),
            _card("c2", "Bob"),
            _card("c3", "Chloé", has_photo=True),
            _card("c4", "Dan", has_photo=True),
        ])
        self.photo_calls: list[list[str]] = []
        self.photos = {"c1": b"\xff\xd8ada", "c3": b"\x89PNGchloe"}
        self.photo_fail = ""

    def card_photos(self, ids):
        self.photo_calls.append(list(ids))
        if self.photo_fail:
            raise RuntimeError(self.photo_fail)
        return {key: self.photos[key] for key in ids if key in self.photos}


def _photo_book(status=STATUS):
    daemon = PhotoDaemon()
    runner = Runner()
    loaded: list[list[str]] = []
    book = ContactBook(
        runner=runner, timer=Timer, fetch_page=daemon.page, fetch_card=daemon.card,
        fetch_photos=daemon.card_photos, on_photos=loaded.append,
    )
    book.apply_status(status)
    book.watch(True)
    runner.run_all()
    return book, daemon, runner, loaded


def test_row_photos_load_only_for_visible_rows_with_a_photo():
    book, daemon, runner, loaded = _photo_book()
    assert daemon.photo_calls == [] and book.row_photo("c1") == b""
    book.request_photos(["c1", "c2", "c3"])
    runner.run_all()
    # Bob has no photo and is never asked for; Dan is not on screen.
    assert daemon.photo_calls == [["c1", "c3"]]
    assert loaded == [["c1", "c3"]]
    assert book.row_photo("c1") == b"\xff\xd8ada" and book.row_photo("c2") == b""
    revision = book.photo_revision
    # Loaded rows and rows without a photo are not read again.
    daemon.photos.pop("c3")
    book.request_photos(["c1", "c3", "c4"])
    runner.run_all()
    assert daemon.photo_calls[-1] == ["c4"]
    assert book.row_photo("c4") == b"" and book.photo_revision == revision
    book.request_photos(["c4"])
    runner.run_all()
    assert len(daemon.photo_calls) == 2


def test_row_photos_are_read_once_while_pending_and_batched():
    from blueferry.limits import MAX_CONTACT_PHOTO_BATCH

    book, daemon, runner, _loaded = _photo_book()
    book.request_photos(["c1"])
    book.request_photos(["c1", "c3"])
    assert len(runner.queue) == 2
    runner.run_all()
    assert daemon.photo_calls == [["c1"], ["c3"]]
    many = [_card(f"x{index}", f"P{index}", has_photo=True) for index in range(120)]
    daemon.cards = many
    book.reload()
    runner.run_all()
    book.request_photos(card["id"] for card in many)
    runner.run_all()
    assert len(daemon.photo_calls[-1]) == MAX_CONTACT_PHOTO_BATCH


def test_row_photos_are_forgotten_with_the_rows_and_on_status_changes():
    book, daemon, runner, _loaded = _photo_book()
    book.request_photos(["c1"])
    runner.run_all()
    assert book.row_photo("c1")
    book.apply_status({**STATUS, "contact_photo_revision": 2})
    assert book.row_photo("c1") == b""
    book.request_photos(["c1"])
    runner.run_all()
    assert book.row_photo("c1")
    book.apply_status({**STATUS, "contact_card_epoch": 8})
    runner.run_all()
    assert book.row_photo("c1") == b""
    book.request_photos(["c1"])
    runner.run_all()
    book.watch(False)
    assert book.row_photo("c1") == b""
    # Hidden: nothing is read.
    calls = len(daemon.photo_calls)
    book.request_photos(["c1"])
    runner.run_all()
    assert len(daemon.photo_calls) == calls


def test_a_late_photo_answer_from_before_an_epoch_change_is_dropped():
    book, _daemon, runner, loaded = _photo_book()
    book.request_photos(["c1"])
    book.apply_status({**STATUS, "contact_card_epoch": 9})
    runner.run_all()
    assert loaded == [] and book.row_photo("c1") == b""


def test_a_failed_photo_read_is_tried_again_and_without_a_fetcher_nothing_loads():
    book, daemon, runner, _loaded = _photo_book()
    daemon.photo_fail = "busy"
    book.request_photos(["c1"])
    runner.run_all()
    assert book.row_photo("c1") == b""
    daemon.photo_fail = ""
    book.request_photos(["c1"])
    runner.run_all()
    assert book.row_photo("c1") and len(daemon.photo_calls) == 2
    plain, _daemon2, plain_runner, _t, _c = _book(PhotoDaemon())
    plain.watch(True)
    plain_runner.run_all()
    plain.request_photos(["c1"])
    assert plain_runner.queue == []


def test_photos_past_the_reply_budget_are_asked_for_again(monkeypatch):
    from blueferry import contacts_model

    monkeypatch.setattr(contacts_model, "MAX_CONTACT_PHOTO_BATCH_BYTES", 1)
    book, daemon, runner, _loaded = _photo_book()
    daemon.photos = {"c1": b"\xff\xd8ada"}
    book.request_photos(["c1", "c3"])
    runner.run_all()
    # c3 may have been left out for the budget: not remembered as photo-less.
    book.request_photos(["c1", "c3"])
    runner.run_all()
    assert daemon.photo_calls == [["c1", "c3"], ["c3"]]
