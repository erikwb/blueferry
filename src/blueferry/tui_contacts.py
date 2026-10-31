"""The contact cards in the Textual client.

:class:`ContactsScreen` (``C``, or **Contacts** in the command palette)
pages through ``Messages1.ListContactCards`` with a search field and shows
the card opened with Enter (``GetContact``): every number, email and postal
address with its label. **Call**, **Send a Message** and **Copy** act on the
selected detail row. Paging, the typing delay, superseded answers, the
``contact_card_epoch`` (a new epoch reloads the list and reads an open card
again) and stale ids come from the shared
:class:`blueferry.contacts_model.ContactBook`, the one the Qt and GTK
clients use; rows, sections and wording from :mod:`blueferry.contacts_view`.
A terminal cannot show the photo, so ``GetContact``'s photo bytes are
ignored.

Every blocking backend call runs off the UI thread, and iPhone text is
rendered as Rich ``Text`` after ``terminal_text``, so it carries neither
markup nor terminal controls.
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

from blueferry import contacts_view, phone_link_view
from blueferry.contacts_model import MAX_QUERY_CHARS, ContactBook
from blueferry.i18n import _
from blueferry.text_safety import terminal_text
from blueferry.tui_tasks import TextualTaskRunner, timer_factory

ContactsResult = tuple[str, str] | None


class ContactsClient(Protocol):
    def contact_cards(
        self, offset: int = 0, limit: int = 150, query: str = "",
    ) -> dict: ...

    def contact(self, contact_id: str) -> tuple[dict, bytes]: ...


def _plain(value: object) -> str:
    return terminal_text(value)


def contact_row_text(row: Mapping[str, Any]) -> Text:
    """One ``list_rows`` row: name and, dimmed, organization and first number."""
    text = Text(_plain(row.get("title")), style="bold")
    subtitle = _plain(row.get("subtitle"))
    if subtitle:
        text.append(f"  {subtitle}", style="dim")
    return text


def detail_row_text(row: Mapping[str, Any]) -> Text:
    """One ``detail_sections`` row: label, then the value (addresses span lines)."""
    label = _plain(row.get("label"))
    lines = _plain(row.get("value")).split("\n")
    text = Text(f"{label}  ", style="#7dd3fc")
    text.append(lines[0])
    indent = " " * (len(label) + 2)
    for line in lines[1:]:
        text.append(f"\n{indent}{line}")
    return text


class ContactItem(ListItem):
    def __init__(self, row: Mapping[str, Any]) -> None:
        self.row = dict(row)
        super().__init__(Static(contact_row_text(row)), classes="contacts-row")
        # A Text, never a str: Textual parses a str tooltip as markup.
        self.tooltip = Text(_plain(row.get("title")))


class MoreItem(ListItem):
    """The last row while the daemon has more matches; loads the next page."""

    def __init__(self) -> None:
        super().__init__(Static(_("Load more…")), classes="contacts-more")


class SectionItem(ListItem):
    def __init__(self, title: str) -> None:
        self.title = _plain(title)
        super().__init__(Static(Text(self.title)), classes="contacts-section", disabled=True)


class DetailItem(ListItem):
    def __init__(self, section: str, row: Mapping[str, Any]) -> None:
        self.section = section
        self.row = dict(row)
        super().__init__(Static(detail_row_text(row)), classes="contacts-detail-row")
        self.tooltip = Text(_plain(f"{row.get('label', '')}, {row.get('value', '')}"))


class ContactsScreen(ModalScreen[ContactsResult]):
    """Browse and search the cached contact cards.

    Dismisses with ``("call", number)`` or ``("message", address)`` for the
    selected detail row, or ``None``.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "Close", show=False),
        Binding("slash", "focus_search", "Search", show=False),
        # Inert while the search field has the focus (it takes the letters).
        Binding("c", "call", "Call", show=False),
        Binding("m", "message", "Send a Message", show=False),
        Binding("y", "copy", "Copy", show=False),
    ]

    def __init__(
        self, client: ContactsClient, *, status: Mapping[str, Any] | None = None,
        open_id: str = "",
    ) -> None:
        super().__init__()
        self._shown = False
        self._status: Mapping[str, Any] = dict(status or {})
        self._open_request = open_id
        self.book = ContactBook(
            runner=TextualTaskRunner(self, group="contacts"),
            timer=timer_factory(self),
            fetch_page=client.contact_cards,
            fetch_card=client.contact,
            on_change=self._book_changed,
        )
        # What the widgets show; the model builds new objects on changes.
        self._shown_rows: list[dict[str, Any]] | None = None
        self._shown_list: tuple[Any, ...] = ()
        self._shown_card: dict[str, Any] | None = None
        self._dirty = False
        self._rendering = False

    def compose(self) -> ComposeResult:
        with Vertical(id="contacts-dialog", classes="dialog"):
            yield Static(_("Contacts"), classes="dialog-title")
            yield Input(
                placeholder=f"{contacts_view.search_placeholder()}  /",
                id="contacts-search", max_length=MAX_QUERY_CHARS,
            )
            yield Static("", id="contacts-count", classes="dialog-copy")
            with Horizontal(id="contacts-body"):
                with Vertical(id="contacts-list-pane"):
                    yield Static(_("Loading…"), id="contacts-state", classes="dialog-copy")
                    yield ListView(id="contacts-list")
                with Vertical(id="contacts-detail-pane"):
                    yield Static("", id="contacts-detail-title")
                    yield Static("", id="contacts-detail-subtitle")
                    yield Static(
                        _("Press Enter on a contact to show its details."),
                        id="contacts-detail-state", classes="dialog-copy",
                    )
                    yield ListView(id="contacts-detail")
            with Horizontal(classes="dialog-actions"):
                yield Button(_("Call  c"), id="contacts-call")
                yield Button(_("Send a Message  m"), id="contacts-message")
                yield Button(_("Copy  y"), id="contacts-copy")
                yield Button(_("Close"), variant="primary", id="contacts-close")

    def on_mount(self) -> None:
        self._shown = True
        self.query_one("#contacts-list", ListView).display = False
        self.query_one("#contacts-detail", ListView).display = False
        self._render_buttons()
        self.book.watch(True)
        self.book.apply_status(self._status)  # reads the first page
        if self._open_request:
            self.open_card(self._open_request)
            self._open_request = ""
        self.query_one("#contacts-search", Input).focus()

    def on_unmount(self) -> None:
        self.book.watch(False)  # forget the cards with the screen

    # -- status --------------------------------------------------------

    def status_changed(self, status: Mapping[str, Any]) -> None:
        self._status = dict(status)
        # A new contact_card_epoch reloads the list and rereads an open card.
        self.book.apply_status(self._status)
        self._render_buttons()

    @property
    def calls_hint(self) -> str:
        return phone_link_view.calls_hint(
            self._status, str(self._status.get("calls_state") or "disabled"),
        )

    # -- model ---------------------------------------------------------

    @property
    def rows(self) -> list[dict[str, Any]]:
        return self.book.rows

    @property
    def card(self) -> dict[str, Any] | None:
        """The loaded open card (``contacts_view.card_view``), else None."""
        card = self.book.card
        return card if card["open"] and card["title"] and not card["stale"] else None

    def reload(self) -> None:
        """Read the first page for the current query again."""
        self.book.reload()

    def load_more(self) -> None:
        self.book.load_more()

    def open_card(self, contact_id: str) -> None:
        """Read one card with ``GetContact``; a stale id says so and reloads."""
        self.book.open(contact_id)

    def _book_changed(self) -> None:
        if not self.is_attached:
            return
        # One render at a time, never cancelled half-way through a ListView
        # update; a change while it runs renders once more afterwards.
        self._dirty = True
        if not self._rendering:
            self._rendering = True
            self.run_worker(self._render_changes(), group="contacts-render")

    async def _render_changes(self) -> None:
        try:
            while self._dirty and self.is_attached:
                self._dirty = False
                view = self.book.view()
                state = (view["loaded"], view["error"], view["count_text"], view["hint"],
                         view["has_more"])
                if view["rows"] is not self._shown_rows or state != self._shown_list:
                    previous = self._shown_rows or []
                    # A further page keeps the highlighted row.
                    appended = bool(previous) and view["rows"][:len(previous)] == previous
                    self._shown_rows, self._shown_list = view["rows"], state
                    await self._render_list(view, keep_index=appended)
                card = self.book.card
                if card is not self._shown_card:
                    # Rereading the same card (a new epoch) keeps the selected row.
                    same = (self._shown_card is not None
                            and self._shown_card["id"] == card["id"])
                    self._shown_card = card
                    await self._render_card(card, keep_index=same)
        finally:
            self._rendering = False

    @property
    def body_text(self) -> str:
        """Everything the list shows, for tests and screen readers."""
        lines = []
        for item in self.query_one("#contacts-list", ListView).children:
            if isinstance(item, ContactItem):
                lines.append(contact_row_text(item.row).plain)
            elif isinstance(item, MoreItem):
                lines.append(_("Load more…"))
        return "\n".join(lines)

    @property
    def detail_text(self) -> str:
        card = self.card or {}
        lines = [_plain(card.get("title", "")), _plain(card.get("subtitle", ""))]
        for item in self.query_one("#contacts-detail", ListView).children:
            if isinstance(item, SectionItem):
                lines.append(item.title)
            elif isinstance(item, DetailItem):
                lines.append(detail_row_text(item.row).plain)
        return "\n".join(line for line in lines if line)

    async def _render_list(self, view: Mapping[str, Any], keep_index: bool) -> None:
        listing = self.query_one("#contacts-list", ListView)
        state = self.query_one("#contacts-state", Static)
        count = self.query_one("#contacts-count", Static)
        index = listing.index if keep_index else None
        await listing.clear()
        if view["error"]:
            state.update(Text(_plain(view["error"])))
            count.update("")
        elif not view["loaded"]:
            state.update(_("Loading…"))
            count.update("")
        else:
            state.update(Text(_plain(view["hint"])))
            count.update(Text(_plain(view["count_text"])))
        items: list[ListItem] = [ContactItem(row) for row in view["rows"]]
        if items and view["has_more"] and not view["error"]:
            items.append(MoreItem())
        if items:
            await listing.extend(items)
            listing.index = min(index or 0, len(items) - 1)
        state.display = not items or bool(view["error"])
        listing.display = bool(items)

    # -- search --------------------------------------------------------

    @on(Input.Changed, "#contacts-search")
    def search_changed(self, event: Input.Changed) -> None:
        self.book.set_query(event.value)  # reloads after the typing delay

    @on(Input.Submitted, "#contacts-search")
    def search_submitted(self) -> None:
        self.book.reload()
        self.query_one("#contacts-list", ListView).focus()

    def action_focus_search(self) -> None:
        self.query_one("#contacts-search", Input).focus()

    # -- card ----------------------------------------------------------

    @on(ListView.Selected, "#contacts-list")
    def contact_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, MoreItem):
            self.load_more()
        elif isinstance(event.item, ContactItem):
            self.open_card(event.item.row["id"])
            self.query_one("#contacts-detail", ListView).focus()

    @on(ListView.Highlighted, "#contacts-list")
    def contact_highlighted(self, event: ListView.Highlighted) -> None:
        # Paging on reaching the end keeps arrow keys enough to see it all.
        if isinstance(event.item, MoreItem):
            self.load_more()

    async def _render_card(self, card: Mapping[str, Any], *, keep_index: bool) -> None:
        title = self.query_one("#contacts-detail-title", Static)
        subtitle = self.query_one("#contacts-detail-subtitle", Static)
        state = self.query_one("#contacts-detail-state", Static)
        details = self.query_one("#contacts-detail", ListView)
        index = details.index if keep_index else None
        await details.clear()
        loaded = card["open"] and bool(card["title"]) and not card["stale"]
        if not loaded:
            title.update("")
            subtitle.update("")
            if not card["open"]:
                note = _("Press Enter on a contact to show its details.")
            else:
                note = card["error"] or (_("Loading…") if card["loading"] else "")
            state.update(Text(_plain(note)))
            state.display = True
            details.display = False
            self._render_buttons()
            return
        title.update(Text(_plain(card["title"]), style="bold"))
        subtitle.update(Text(_plain(card["subtitle"]), style="dim"))
        items: list[ListItem] = []
        for section in card["sections"]:
            items.append(SectionItem(section["title"]))
            items.extend(DetailItem(section["id"], row) for row in section["rows"])
        # A failed reread keeps the card and says why above it.
        note = card["error"] or (
            "" if items else _("This card has no numbers or addresses."))
        state.update(Text(_plain(note)))
        state.display = bool(note)
        if items:
            await details.extend(items)
            details.index = min(index or 1, len(items) - 1)
        details.display = bool(items)
        self._render_buttons()

    # -- actions -------------------------------------------------------

    def _selected_row(self) -> dict[str, Any] | None:
        item = self.query_one("#contacts-detail", ListView).highlighted_child
        return item.row if isinstance(item, DetailItem) else None

    def _can(self, action: str) -> bool:
        row = self._selected_row()
        return bool(row and row.get("target") and action in row.get("actions", ()))

    def _render_buttons(self) -> None:
        if not self._shown or not self.is_attached:
            return
        call = self.query_one("#contacts-call", Button)
        hint = self.calls_hint
        call.disabled = not self._can(contacts_view.ACTION_DIAL) or bool(hint)
        call.tooltip = Text(_plain(hint)) if hint and self._can(contacts_view.ACTION_DIAL) else None
        self.query_one("#contacts-message", Button).disabled = not self._can(
            contacts_view.ACTION_MESSAGE,
        )
        self.query_one("#contacts-copy", Button).disabled = not self._can(
            contacts_view.ACTION_COPY,
        )

    @on(ListView.Highlighted, "#contacts-detail")
    def detail_highlighted(self) -> None:
        self._render_buttons()

    @on(ListView.Selected, "#contacts-detail")
    def detail_selected(self) -> None:
        # Enter does the row's first action the terminal offers.
        if self._can(contacts_view.ACTION_DIAL) and not self.calls_hint:
            self.action_call()
        elif self._can(contacts_view.ACTION_MESSAGE):
            self.action_message()
        else:
            self.action_copy()

    def _typing_in_search(self) -> bool:
        return isinstance(self.focused, Input)

    @on(Button.Pressed, "#contacts-call")
    def call_button(self) -> None:
        self.action_call()

    def action_call(self) -> None:
        if self._typing_in_search() or not self._can(contacts_view.ACTION_DIAL):
            return
        hint = self.calls_hint
        if hint:
            self.notify(_plain(hint), severity="warning", markup=False)
            return
        row = self._selected_row() or {}
        self.dismiss(("call", phone_link_view.dialable(str(row["target"]))))

    @on(Button.Pressed, "#contacts-message")
    def message_button(self) -> None:
        self.action_message()

    def action_message(self) -> None:
        if self._typing_in_search() or not self._can(contacts_view.ACTION_MESSAGE):
            return
        row = self._selected_row() or {}
        self.dismiss(("message", str(row["target"])))

    @on(Button.Pressed, "#contacts-copy")
    def copy_button(self) -> None:
        self.action_copy()

    def action_copy(self) -> None:
        if self._typing_in_search() or not self._can(contacts_view.ACTION_COPY):
            return
        row = self._selected_row() or {}
        # OSC 52, like Copy Report in diagnostics; terminals may ignore it.
        self.app.copy_to_clipboard(str(row["target"]))
        self.notify(_("Copied"), markup=False)

    @on(Button.Pressed, "#contacts-close")
    def close_button(self) -> None:
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)
