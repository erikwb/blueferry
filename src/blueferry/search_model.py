"""Toolkit-neutral state of the global search (``Messages1.Search``).

Shared by the Qt client (``qt/controller.py``), the GTK client
(``ui/search.py``) and the terminal client (``tui_search.py``); sections,
rows and wording come from
:mod:`blueferry.search_view`. The search runs
:data:`blueferry.search_view.TYPING_DELAY_MS` after the last change of the
query, only one answer (the newest) is shown, and the results are dropped
when the search is closed (:meth:`GlobalSearch.clear`). While the daemon's
search queue is full it answers ``NotReady``; the model then tries the same
query again after the typing delay, :data:`BUSY_RETRIES` times, and then
says the search is busy.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from blueferry import search_view
from blueferry.client_tasks import TaskRunner, TimerFactory

# Results asked for per group; the daemon caps it (search_result_limit).
LIMIT = 20
MAX_QUERY_CHARS = 128
BUSY_RETRIES = 3

_BUSY = object()


def _busy(error: BaseException) -> bool:
    """Whether ``error`` is the daemon's ``NotReady`` (search queue full)."""
    for item in (error, error.__cause__):
        get_name = getattr(item, "get_dbus_name", None)
        name = get_name() if callable(get_name) else getattr(item, "dbus_name", "")
        if str(name or "").endswith(".NotReady"):
            return True
    return False


class GlobalSearch:
    def __init__(
        self,
        *,
        runner: TaskRunner,
        timer: TimerFactory,
        fetch: Callable[[str, int], object],
        on_change: Callable[[], None] = lambda: None,
    ) -> None:
        """``fetch(query, limit)`` is the client's blocking ``Search`` call;
        it runs on ``runner`` (the client's D-Bus worker)."""
        self._runner = runner
        self._fetch = fetch
        self._on_change = on_change
        self._timer = timer(self.run)
        self._supported = False
        self._query = ""
        self._serial = 0
        self._retries = 0
        self._payload: object = {}
        self._searched = ""
        self._loading = False
        self._error = ""

    @property
    def supported(self) -> bool:
        return self._supported

    @property
    def query(self) -> str:
        return self._query

    def view(self) -> dict[str, Any]:
        """``search_view.search_view`` of the shown answer, plus the state."""
        # The previous answer stays listed while the next one is on its way.
        shown = search_view.should_search(self._query) and bool(self._searched)
        payload = self._payload if shown else {"query": self._query}
        view = search_view.search_view(payload)
        # Also say so when the daemon capped the LIMIT this model asked for.
        view["limit_text"] = search_view.limit_text(payload, LIMIT)
        if self._loading:
            view["hint"] = ""
        if self._error:
            view = {**view, "sections": [], "count": 0, "hint": ""}
        return {
            **view,
            "supported": self._supported,
            "query": self._query,
            "loading": self._loading,
            "error": self._error,
            "placeholder": search_view.placeholder(),
            "scope": search_view.scope_text(),
        }

    def target_at(self, index: int) -> dict[str, Any] | None:
        """Where row ``index`` of :func:`search_view.flat_rows` lives."""
        if not self._searched or self._error:
            return None
        return search_view.target_at(self._payload, index)

    def apply_status(self, status: Mapping[str, Any]) -> None:
        supported = search_view.supported(status)
        if supported != self._supported:
            self._supported = supported
            if not supported:
                self.clear()
            self._on_change()

    def set_query(self, text: str) -> None:
        """The search entry changed: search after a short pause."""
        query = " ".join(str(text or "").split())[:MAX_QUERY_CHARS]
        if query == self._query:
            return
        self._query = query
        self._retries = 0
        self._serial += 1
        self._error = ""
        if search_view.should_search(query) and self._supported:
            self._loading = True
            self._timer.start(search_view.TYPING_DELAY_MS)
        else:
            self._timer.stop()
            self._loading = False
            self._payload, self._searched = {}, ""
        self._on_change()

    def run(self) -> None:
        """Search for the current query now."""
        self._timer.stop()
        query = self._query
        if not (self._supported and search_view.should_search(query)):
            return
        self._serial += 1
        serial = self._serial
        fetch = self._fetch
        self._loading = True

        def operation() -> object:
            try:
                return fetch(query, LIMIT)
            except Exception as error:
                if _busy(error):
                    return _BUSY
                raise

        def completed(value: object) -> None:
            if serial != self._serial:
                return
            if value is _BUSY:
                if self._retries < BUSY_RETRIES:
                    self._retries += 1
                    self._timer.start(search_view.TYPING_DELAY_MS)
                    return
                self._loading = False
                self._error = search_view.search_busy_text()
                self._on_change()
                return
            self._loading = False
            self._error = ""
            self._payload, self._searched = value, query
            self._on_change()

        def failed(message: str) -> None:
            if serial != self._serial:
                return
            self._loading = False
            self._error = search_view.search_failed_text(message)
            self._on_change()

        self._runner.submit(operation, completed, failed)

    def clear(self) -> None:
        """Close the search: forget the query and every result."""
        self._timer.stop()
        self._serial += 1
        changed = bool(self._query or self._searched or self._error or self._loading)
        self._query, self._payload, self._searched = "", {}, ""
        self._loading, self._error, self._retries = False, "", 0
        if changed:
            self._on_change()
