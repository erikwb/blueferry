"""The global search in the Textual client.

:class:`SearchScreen` (``f``, or **Search Everything** in the command
palette) sends ``Messages1.Search`` a short moment after the last key press
and lists the hits grouped into conversations, messages, contacts and calls,
with the matching words of a message highlighted. Enter on a hit closes the
screen with its ``target``; the app then opens the conversation, the
contact card or the recent calls at that call. Sections, rows, hints and
wording come from the shared :mod:`blueferry.search_view`.

The typing delay, superseded answers and the daemon's ``NotReady`` (tried
again after the typing delay, a few times, then "busy") come from the
shared :class:`blueferry.search_model.GlobalSearch`, the one the Qt and GTK
clients use. Every blocking backend call runs off the UI thread, and iPhone
text is rendered as Rich ``Text`` after ``terminal_text``.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar, Protocol

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, ListItem, ListView, Static

from blueferry import search_view
from blueferry.i18n import _
from blueferry.search_model import MAX_QUERY_CHARS, GlobalSearch
from blueferry.text_safety import terminal_text
from blueferry.tui_tasks import TextualTaskRunner, timer_factory

SearchResult = dict[str, Any] | None


class SearchClient(Protocol):
    def search(self, query: str, limit: int = 20) -> dict: ...


def _plain(value: object) -> str:
    return terminal_text(value)


def _one_line(value: object) -> str:
    return " ".join(_plain(value).split())


def result_text(row: Mapping[str, Any]) -> Text:
    """One ``search_view`` row: title, subtitle and time, then the snippet."""
    text = Text(_one_line(row.get("title")), style="bold")
    meta = "  ·  ".join(
        value for value in (_one_line(row.get("subtitle")), _one_line(row.get("time")))
        if value
    )
    if meta:
        text.append(f"  {meta}", style="dim")
    snippet = row.get("snippet")
    if isinstance(snippet, str) and snippet:
        # terminal_text keeps the length, so the match offsets still fit.
        safe = _plain(snippet).replace("\n", " ")
        line = Text(f"\n  {safe}")
        highlight = row.get("highlight")
        if isinstance(highlight, tuple | list) and len(highlight) == 2:
            start, length = highlight
            line.stylize("bold reverse", 3 + start, 3 + start + length)
        text.append_text(line)
    return text


class SectionItem(ListItem):
    def __init__(self, section: Mapping[str, Any]) -> None:
        self.title = _plain(section["title"])
        self.more = _plain(section.get("more") or "")
        label = Text(self.title)
        if self.more:
            label.append(f"  ({self.more})", style="dim")
        super().__init__(Static(label), classes="search-section", disabled=True)


class ResultItem(ListItem):
    def __init__(self, section: str, row: Mapping[str, Any]) -> None:
        self.section = section
        self.row = dict(row)
        super().__init__(Static(result_text(row)), classes="search-row")
        # A Text, never a str: Textual parses a str tooltip as markup.
        self.tooltip = Text(_one_line(f"{row.get('title', '')}, {row.get('subtitle', '')}"))


class SearchScreen(ModalScreen[SearchResult]):
    """Search messages, contacts and calls; dismisses with the hit's target."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "Close", show=False),
        Binding("slash", "focus_search", "Search", show=False),
    ]

    def __init__(
        self, client: SearchClient, *, status: Mapping[str, Any] | None = None,
        query: str = "",
    ) -> None:
        super().__init__()
        self._initial = query
        self._status: Mapping[str, Any] = dict(status or {})
        self.model = GlobalSearch(
            runner=TextualTaskRunner(self, group="search"),
            timer=timer_factory(self),
            fetch=client.search,
            on_change=self._changed,
        )

    def compose(self) -> ComposeResult:
        with Vertical(id="search-dialog", classes="dialog"):
            yield Static(_("Search"), classes="dialog-title")
            yield Input(
                value=self._initial, placeholder=search_view.placeholder(),
                id="search-input", max_length=MAX_QUERY_CHARS,
            )
            yield Static("", id="search-state", classes="dialog-copy")
            yield Static("", id="search-limit", classes="dialog-copy")
            yield ListView(id="search-results")
            with Horizontal(classes="dialog-actions"):
                yield Button(_("Open"), id="search-open", disabled=True)
                yield Button(_("Close"), variant="primary", id="search-close")

    def on_mount(self) -> None:
        self.query_one("#search-results", ListView).display = False
        self.query_one("#search-limit", Static).display = False
        self.query_one("#search-input", Input).focus()
        self.model.apply_status(self._status)
        self.model.set_query(self._initial)
        if search_view.should_search(self.model.query):
            self.model.run()  # a query handed in is searched at once
        self._changed()

    def on_unmount(self) -> None:
        self.model.clear()  # forget the results with the screen

    def status_changed(self, status: Mapping[str, Any]) -> None:
        self._status = dict(status)
        self.model.apply_status(self._status)

    @property
    def query_text(self) -> str:
        return self.model.query

    # -- searching -----------------------------------------------------

    @on(Input.Changed, "#search-input")
    def query_changed(self, event: Input.Changed) -> None:
        self.model.set_query(event.value)  # searches after the typing delay

    @on(Input.Submitted, "#search-input")
    def query_submitted(self) -> None:
        if search_view.should_search(self.model.query):
            self.model.run()
        results = self.query_one("#search-results", ListView)
        if results.display:
            results.focus()

    def search_now(self) -> None:
        self.model.run()

    def _changed(self) -> None:
        if self.is_attached:
            self.run_worker(self._render_results(), group="search-render", exclusive=True)

    # -- rendering -----------------------------------------------------

    @property
    def body_text(self) -> str:
        """Everything the result list shows, for tests and screen readers."""
        lines = []
        for item in self.query_one("#search-results", ListView).children:
            if isinstance(item, SectionItem):
                lines.append(f"{item.title}  ({item.more})" if item.more else item.title)
            elif isinstance(item, ResultItem):
                lines.append(result_text(item.row).plain)
        return "\n".join(lines)

    async def _render_results(self) -> None:
        state = self.query_one("#search-state", Static)
        limit = self.query_one("#search-limit", Static)
        results = self.query_one("#search-results", ListView)
        view = self.model.view()
        await results.clear()
        items: list[ListItem] = []
        for section in view["sections"]:
            items.append(SectionItem(section))
            items.extend(ResultItem(section["id"], row) for row in section["rows"])
        if view["error"]:
            message = view["error"]
        elif not search_view.should_search(view["query"]):
            message = f"{view['hint']} {view['scope']}"
        else:
            message = view["hint"]
        state.update(Text(_plain(message)))
        state.display = bool(message)
        limit_text = "" if view["error"] else view["limit_text"]
        limit.update(Text(_plain(limit_text)))
        limit.display = bool(limit_text)
        if items:
            await results.extend(items)
            results.index = 1
        results.display = bool(items)
        self.query_one("#search-open", Button).disabled = not items

    # -- choosing a hit ------------------------------------------------

    def _selected_target(self) -> dict[str, Any] | None:
        item = self.query_one("#search-results", ListView).highlighted_child
        if isinstance(item, ResultItem):
            target = item.row.get("target")
            return dict(target) if isinstance(target, Mapping) else None
        return None

    @on(ListView.Selected, "#search-results")
    def result_selected(self) -> None:
        self.open_selected()

    @on(Button.Pressed, "#search-open")
    def open_button(self) -> None:
        self.open_selected()

    def open_selected(self) -> None:
        target = self._selected_target()
        if target is not None:
            self.dismiss(target)

    def action_focus_search(self) -> None:
        self.query_one("#search-input", Input).focus()

    @on(Button.Pressed, "#search-close")
    def close_button(self) -> None:
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)
