"""Toolkit-neutral state of the Contacts tab (``ListContactCards``/``GetContact``).

Shared by the Qt client (``qt/controller.py``), the GTK client
(``ui/contacts.py``) and the terminal client (``tui_contacts.py``); wording
and row shapes come from
:mod:`blueferry.contacts_view`. Contact cards are private, so the list is
read only while the tab is shown and forgotten, with an open card, when it
is hidden. Typing in the search field reloads the first page after
:data:`blueferry.search_view.TYPING_DELAY_MS`; further pages load on
demand (:meth:`ContactBook.load_more`). Card ids are only valid within one
``contact_card_epoch``: when :meth:`ContactBook.apply_status` sees the epoch
change, a shown list is reloaded and an open card is read again, which
answers "changed or removed" (``NotFound``) for a stale id.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from blueferry import contacts_view
from blueferry.client import ContactNotFoundError
from blueferry.client_tasks import TaskRunner, TimerFactory
from blueferry.search_view import TYPING_DELAY_MS

PAGE_SIZE = contacts_view.DEFAULT_PAGE
# Longest query sent to the daemon; the search field allows no more.
MAX_QUERY_CHARS = 128

_STALE = object()


def _empty_card() -> dict[str, Any]:
    return {
        "open": False, "id": "", "loading": False, "stale": False, "error": "",
        "title": "", "subtitle": "", "initials": "", "has_photo": False,
        "photo_retry": False, "sections": [],
    }


class ContactBook:
    def __init__(
        self,
        *,
        runner: TaskRunner,
        timer: TimerFactory,
        fetch_page: Callable[[int, int, str], object],
        fetch_card: Callable[[str], tuple[object, bytes]],
        on_change: Callable[[], None] = lambda: None,
    ) -> None:
        """``fetch_page(offset, limit, query)`` and ``fetch_card(id)`` are the
        client's blocking ``ListContactCards``/``GetContact`` calls; they run
        on ``runner`` (the client's D-Bus worker)."""
        self._runner = runner
        self._fetch_page = fetch_page
        self._fetch_card = fetch_card
        self._on_change = on_change
        self._timer = timer(self.reload)
        self._watched = False
        self._supported = False
        self._epoch: object = None
        self._query = ""
        self._list_serial = 0
        self._card_serial = 0
        self._rows: list[dict[str, Any]] = []
        self._page: object = {}
        self._loaded = False
        self._loading = False
        self._error = ""
        self._card = _empty_card()
        self._photo = b""

    # ---- state ----------------------------------------------------------

    @property
    def watched(self) -> bool:
        return self._watched

    @property
    def supported(self) -> bool:
        return self._supported

    @property
    def query(self) -> str:
        return self._query

    @property
    def rows(self) -> list[dict[str, Any]]:
        """``contacts_view.list_rows`` of every page loaded so far."""
        return self._rows

    @property
    def card(self) -> dict[str, Any]:
        """The open card: ``contacts_view.card_view`` plus ``open``, ``id``,
        ``loading``, ``stale`` and ``error``."""
        return self._card

    @property
    def photo(self) -> bytes:
        """Photo bytes of the open card (undecoded, untrusted), or ``b""``."""
        return self._photo

    def view(self) -> dict[str, Any]:
        """Everything the list shows: rows, count and empty texts, paging."""
        loaded = self._loaded
        return {
            "supported": self._supported,
            "loaded": loaded,
            "loading": self._loading,
            "query": self._query,
            "rows": self._rows,
            "count": len(self._rows),
            "count_text": contacts_view.count_text(self._page) if loaded else "",
            "hint": contacts_view.empty_hint(self._page) if loaded and not self._error else "",
            "has_more": loaded and contacts_view.has_more(self._page),
            "error": self._error,
            "placeholder": contacts_view.search_placeholder(),
        }

    # ---- inputs ---------------------------------------------------------

    def apply_status(self, status: Mapping[str, Any]) -> None:
        """A GetStatus answer: capability and card epoch."""
        supported = contacts_view.supported(status)
        epoch = status.get(contacts_view.EPOCH_KEY) if supported else None
        changed = epoch != self._epoch
        previous, self._epoch = self._epoch, epoch
        if supported != self._supported:
            self._supported = supported
            if supported:
                self.reload()
            else:
                self._forget()
            return
        if changed and previous is not None and supported:
            self.reload()
            if self._card["open"]:
                self.open(self._card["id"])

    def watch(self, watched: bool) -> None:
        """The tab became visible (load) or hidden (forget rows and card)."""
        watched = bool(watched)
        if watched == self._watched:
            return
        self._watched = watched
        if watched:
            self.reload()
        else:
            self._forget()

    def set_query(self, text: str) -> None:
        """The search field changed: reload the first page after a pause."""
        query = " ".join(str(text or "").split())[:MAX_QUERY_CHARS]
        if query == self._query:
            return
        self._query = query
        if self._watched and self._supported:
            self._timer.start(TYPING_DELAY_MS)

    def reload(self) -> None:
        """Read the first page for the current query now."""
        self._timer.stop()
        if not (self._watched and self._supported):
            return
        self._load(0, replace=True)

    def load_more(self) -> None:
        """Append the next page, when there is one and none is loading."""
        if not (self._watched and self._supported and self._loaded) or self._loading:
            return
        if not contacts_view.has_more(self._page):
            return
        self._load(contacts_view.next_offset(self._page), replace=False)

    def open(self, contact_id: str) -> None:
        """Read one card with its photo; a stale id is reported, not raised."""
        contact_id = str(contact_id or "")
        if not contact_id or not self._watched:
            return
        self._card_serial += 1
        serial = self._card_serial
        # Reading the open card again keeps it on screen until the answer.
        same = self._card["open"] and self._card["id"] == contact_id
        if not same:
            self._photo = b""
        base = self._card if same else _empty_card()
        self._card = {**base, "open": True, "id": contact_id, "loading": True,
                      "stale": False, "error": ""}
        self._on_change()
        fetch = self._fetch_card

        def operation() -> object:
            try:
                return fetch(contact_id)
            except ContactNotFoundError:
                return _STALE

        def completed(value: object) -> None:
            if serial != self._card_serial or not self._watched:
                return
            if value is _STALE:
                self._photo = b""
                self._card = {**_empty_card(), "open": True, "id": contact_id,
                              "stale": True, "error": contacts_view.stale_text()}
                # The list holds ids of the same run: read it again too.
                self.reload()
            else:
                document, photo = value if isinstance(value, tuple) else ({}, b"")
                self._photo = photo if isinstance(photo, bytes) else b""
                self._card = {**_empty_card(), **contacts_view.card_view(document),
                              "open": True, "id": contact_id}
            self._on_change()

        def failed(message: str) -> None:
            if serial != self._card_serial or not self._watched:
                return
            self._card = {**self._card, "loading": False,
                          "error": contacts_view.load_failed_text(message)}
            self._on_change()

        self._runner.submit(operation, completed, failed)

    def close(self) -> None:
        """Leave the card view (and drop its photo)."""
        self._card_serial += 1
        if self._card["open"] or self._photo:
            self._card = _empty_card()
            self._photo = b""
            self._on_change()

    # ---- internals ------------------------------------------------------

    def _forget(self) -> None:
        self._timer.stop()
        self._list_serial += 1
        self._card_serial += 1
        changed = bool(self._rows or self._loaded or self._error or self._card["open"])
        self._rows, self._page, self._loaded = [], {}, False
        self._loading, self._error = False, ""
        self._card, self._photo = _empty_card(), b""
        if changed:
            self._on_change()

    def _load(self, offset: int, *, replace: bool) -> None:
        self._list_serial += 1
        serial = self._list_serial
        query = self._query
        fetch = self._fetch_page
        self._loading = True
        self._on_change()

        def completed(value: object) -> None:
            if serial != self._list_serial or not self._watched:
                return
            self._loading = False
            self._error = ""
            page = contacts_view.normalize_page(value)
            rows = contacts_view.list_rows(page)
            if replace:
                self._rows = rows
            else:
                seen = {row["id"] for row in self._rows}
                self._rows = self._rows + [row for row in rows if row["id"] not in seen]
            self._page = page
            self._loaded = True
            self._on_change()

        def failed(message: str) -> None:
            if serial != self._list_serial or not self._watched:
                return
            self._loading = False
            if replace:
                self._rows, self._page, self._loaded = [], {}, False
            self._error = contacts_view.load_failed_text(message)
            self._on_change()

        self._runner.submit(lambda: fetch(offset, PAGE_SIZE, query), completed, failed)
