"""Optional phone-call panel for the Textual client (Calls1).

The panel is a modal screen opened with ``c``. It reads ListCalls while it
is open (a poll plus ``CallsChanged``) and runs every blocking backend call
through :class:`~blueferry.tui_tasks.TextualTaskRunner`, so the terminal
never waits on oFono or the phone and an answer arriving after the panel
closed is dropped.

The wording is shared with the Qt and GTK Calls tabs
(``phone_link_view.call_card``, ``calls_hint``, ``dial_suggestions``). Where
those show a card with buttons per call, the terminal keeps one action row
whose buttons pick the obvious call (answer the ringing one, hold or resume
the connected one, hang up the active one first): fewer widgets to tab
through, the same actions. Keypad tones go to the active call. Names,
numbers and errors are rendered as Rich ``Text`` after ``terminal_text``.
"""
from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping
from typing import Any, ClassVar, Protocol

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Static
from textual.widgets.option_list import Option

from blueferry import phone_link_view
from blueferry.calls.model import MAX_DTMF_TONES
from blueferry.conversation_state import ConversationState
from blueferry.i18n import _
from blueferry.models import CALLS_STATE_TEXT, CallInfo, CallsSnapshot
from blueferry.text_safety import terminal_text
from blueferry.tui_tasks import TextualTaskRunner, TextualTimer

_POLL_SECONDS = 2.0
_SEARCH_DELAY_MS = 200
_TICK_SECONDS = 1.0
# The dial field's length, as in the Qt and GTK clients.
MAX_QUERY = 64
_RINGING = frozenset({"incoming", "waiting"})
_NOT_TONE = re.compile(r"[^0-9*#]")
# calls_hint covers these states; others (bluez_conflict) keep their own text.
_HINTED_STATES = frozenset({"ready", "connecting", "searching", "unavailable", "disabled"})


class CallsClient(Protocol):
    def calls(self) -> CallsSnapshot: ...

    def dial(self, number: str) -> str: ...

    def answer_call(self, call_id: str) -> None: ...

    def hangup_call(self, call_id: str) -> None: ...

    def swap_calls(self) -> None: ...

    def send_call_tones(self, call_id: str, tones: str) -> None: ...

    def find_contacts(self, query: str) -> list[tuple[str, str]]: ...


def _plain(value: object) -> str:
    return terminal_text(value).replace("\n", " ")


def tones(value: object) -> str:
    """What the tone field keeps: 0-9, ``*`` and ``#``, as the daemon takes."""
    return _NOT_TONE.sub("", str(value or ""))[:MAX_DTMF_TONES]


def calls_text(snapshot: CallsSnapshot, now: float) -> Text:
    """The running calls as ``call_card`` titles and subtitles, plain text."""
    if not snapshot.calls:
        return Text(_("No active calls"))
    text = Text()
    for index, call in enumerate(snapshot.calls):
        if index:
            text.append("\n")
        card = phone_link_view.call_card(call.to_dict(), now)
        text.append(_plain(card["title"]), style="bold")
        number = _plain(call.number)
        if number and number != _plain(card["title"]):
            text.append(f"  {number}", style="dim")
        if card["subtitle"]:
            text.append(f"  ·  {_plain(card['subtitle'])}")
    return text


def state_text(status: Mapping[str, Any], snapshot: CallsSnapshot) -> str:
    """Why calls cannot be placed yet; empty when they can."""
    if status.get("calls_enabled") is True and snapshot.state not in _HINTED_STATES:
        known = CALLS_STATE_TEXT.get(snapshot.state)
        if known:
            return known
    return phone_link_view.calls_hint(status, snapshot.state)


def ringing_call(snapshot: CallsSnapshot) -> str | None:
    ringing = [call.call_id for call in snapshot.calls if call.state in _RINGING]
    return ringing[0] if len(ringing) == 1 else None


_HANGUP_PRIORITY = (
    ("active",), ("held",), ("incoming", "waiting"), ("dialing", "alerting"),
)


def hangup_target(snapshot: CallsSnapshot) -> CallInfo | None:
    """Pick the one call the hang-up button ends: active, then held.

    A ringing call is declined only when nothing is connected, and the panel
    never falls back to "hang up all" (neither do the Qt and GTK clients).
    """
    for states in _HANGUP_PRIORITY:
        for call in snapshot.calls:
            if call.state in states:
                return call
    return None


def hold_target(snapshot: CallsSnapshot) -> CallInfo | None:
    """The call Hold/Resume (``SwapCalls``) acts on: active, else held."""
    for state in ("active", "held"):
        for call in snapshot.calls:
            if call.state == state:
                return call
    return None


def active_call(snapshot: CallsSnapshot) -> CallInfo | None:
    """The call keypad tones go to."""
    return next((call for call in snapshot.calls if call.state == "active"), None)


class DialConfirmScreen(ModalScreen[bool]):
    """Ask before a number is dialed; a stray Enter must not place a call."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, number: str) -> None:
        super().__init__()
        self.number = number

    def compose(self) -> ComposeResult:
        shown = _plain(self.number)
        with Vertical(id="calls-confirm-dialog", classes="dialog"):
            yield Static(_("Place call?"), classes="dialog-title")
            yield Static(
                Text(_("Call {number} through the iPhone?").format(number=shown)),
                classes="dialog-copy",
            )
            with Horizontal(classes="dialog-actions"):
                yield Button(_("Cancel"), id="calls-confirm-cancel")
                yield Button(_("Call"), variant="primary", id="calls-confirm-call")

    def on_mount(self) -> None:
        self.query_one("#calls-confirm-cancel", Button).focus()

    @on(Button.Pressed, "#calls-confirm-cancel")
    def cancel_button(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#calls-confirm-call")
    def call_button(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class CallsScreen(ModalScreen[None]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "Close", show=False),
    ]

    def __init__(
        self,
        client: CallsClient,
        *,
        status: Mapping[str, Any],
        number: str = "",
        poll_seconds: float = _POLL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__()
        self._shown = False  # composed and mounted; widgets exist
        self._client = client
        self._status: Mapping[str, Any] = dict(status)
        self._initial_number = phone_link_view.dialable(number)
        self._poll_seconds = poll_seconds
        self._clock = clock
        # The status's Calls1 state until ListCalls answers, so a call back
        # can ask for confirmation right away.
        self.snapshot = CallsSnapshot(state=str(status.get("calls_state") or "unknown"))
        self.loaded = False
        self.busy = False
        self._refreshing = False
        self._refresh_again = False
        self._contacts = ConversationState(select_first=False)
        # The text a picked suggestion put into the field: no new search.
        self._filled: str | None = None
        self._suggestions: list[str] = []
        self._runner = TextualTaskRunner(self, group="calls")
        self._search_runner = TextualTaskRunner(self, group="calls-search")
        self._search_timer = TextualTimer(self, self._search_contacts)

    def compose(self) -> ComposeResult:
        with Vertical(id="calls-dialog", classes="dialog"):
            yield Static(_("Phone calls (experimental)"), classes="dialog-title")
            yield Static(_("Loading…"), id="calls-state")
            yield Static("", id="calls-list", classes="dialog-copy")
            with Horizontal(id="calls-tones-row"):
                yield Input(
                    placeholder=_("Keypad tones (0-9 * #)"), id="calls-tones",
                    max_length=MAX_DTMF_TONES, restrict=r"[0-9*#]*",
                )
                yield Button(_("Send Tones"), id="calls-tones-send")
            yield Static(_("Number or name"), classes="field-label")
            yield Input(
                value=self._initial_number, placeholder="+1 555 123 4567",
                id="calls-number", max_length=MAX_QUERY,
            )
            yield OptionList(id="calls-suggestions")
            with Horizontal(classes="dialog-actions"):
                yield Button(_("Close"), id="calls-close")
                yield Button(_("Hang Up"), variant="error", id="calls-hangup")
                yield Button(_("Hold"), id="calls-hold")
                yield Button(_("Answer"), variant="success", id="calls-answer")
                yield Button(_("Call"), variant="primary", id="calls-dial")

    def on_mount(self) -> None:
        self._shown = True
        if self._initial_number:
            self._filled = self._initial_number
            self.call_after_refresh(self.dial)
        self.query_one("#calls-suggestions", OptionList).display = False
        self.query_one("#calls-number", Input).focus()
        self._update_view()
        self.refresh_calls()
        self.set_interval(self._poll_seconds, self.refresh_calls)
        self.set_interval(_TICK_SECONDS, self._tick)

    # ---- the app's side -------------------------------------------------

    def status_changed(self, status: Mapping[str, Any]) -> None:
        """A new GetStatus: the readiness hint and buttons follow it."""
        self._status = dict(status)
        self._update_view()

    @property
    def hint(self) -> str:
        return state_text(self._status, self.snapshot)

    # ---- loading --------------------------------------------------------

    def refresh_calls(self) -> None:
        """Read ListCalls once; a request while one runs reads once more after."""
        if self._refreshing:
            self._refresh_again = True
            return
        self._refreshing = True
        self._runner.submit(
            self._client.calls, self._show, self._show_error, self._refresh_finished,
        )

    def _refresh_finished(self) -> None:
        self._refreshing = False
        if self._refresh_again:
            self._refresh_again = False
            self.refresh_calls()

    def _show(self, snapshot: object) -> None:
        if not isinstance(snapshot, CallsSnapshot):
            return
        self.snapshot, self.loaded = snapshot, True
        self._update_view()

    def _show_error(self, message: str) -> None:
        # No stale calls or buttons after ListCalls failed.
        self.snapshot, self.loaded = CallsSnapshot(state="unavailable"), True
        self._update_view(error=message or _("Phone calls are unavailable"))

    # ---- rendering ------------------------------------------------------

    def _update_view(self, *, error: str = "") -> None:
        if not self._shown or not self.is_attached:
            return
        snapshot = self.snapshot
        state = self.query_one("#calls-state", Static)
        list_view = self.query_one("#calls-list", Static)
        if not self.loaded:
            state.update(_("Loading…"))
            list_view.update("")
        else:
            # Caller names and numbers come from the phone and contacts.
            # Render them as plain Text: a str would be parsed as Textual
            # markup, so a contact named "[@click=app.quit]x[/]" would
            # become an action link.
            state.update(Text(_plain(error or self.hint)))
            list_view.update(calls_text(snapshot, self._clock()))
        state.display = bool(error or self.hint) or not self.loaded
        self._render_buttons(failed=bool(error))

    def _render_buttons(self, *, failed: bool = False) -> None:
        snapshot, busy = self.snapshot, self.busy or failed
        answer = self.query_one("#calls-answer", Button)
        answer.disabled = busy or ringing_call(snapshot) is None
        target = hangup_target(snapshot)
        hangup = self.query_one("#calls-hangup", Button)
        hangup.disabled = busy or target is None
        now = self._clock()
        hangup.label = (
            phone_link_view.call_card(target.to_dict(), now)["hangup"]
            if target is not None else _("Hang Up")
        )
        held = hold_target(snapshot)
        hold = self.query_one("#calls-hold", Button)
        hold.disabled = busy or held is None
        hold.label = (
            phone_link_view.call_card(held.to_dict(), now)["hold"]
            if held is not None else _("Hold")
        )
        active = active_call(snapshot)
        self.query_one("#calls-tones-row").display = active is not None
        self.query_one("#calls-tones-send", Button).disabled = busy or active is None
        self.query_one("#calls-dial", Button).disabled = busy or bool(self.hint)

    def _tick(self) -> None:
        """The active call's duration, without reading ListCalls."""
        if self.loaded and self.snapshot.calls:
            self.query_one("#calls-list", Static).update(
                calls_text(self.snapshot, self._clock())
            )

    # ---- actions --------------------------------------------------------

    def _act(self, operation: Callable[[], object], done: str) -> None:
        if self.busy:
            return
        self.busy = True
        self._render_buttons()

        def succeeded(_value: object) -> None:
            self.notify(done, markup=False)

        def failed(message: str) -> None:
            self.notify(_plain(message), severity="error", markup=False)

        def finished() -> None:
            self.busy = False
            self._render_buttons()
            self.refresh_calls()

        self._runner.submit(operation, succeeded, failed, finished)

    @on(Button.Pressed, "#calls-dial")
    @on(Input.Submitted, "#calls-number")
    def dial(self) -> None:
        number = phone_link_view.dialable(self.query_one("#calls-number", Input).value)
        if not number:
            self.notify(_("Enter a number to dial"), severity="warning", markup=False)
            return
        hint = self.hint
        if hint:
            self.notify(_plain(hint), severity="warning", markup=False)
            return

        def confirmed(accepted: bool | None) -> None:
            if accepted:
                self._act(lambda: self._client.dial(number), _("Calling…"))

        self.app.push_screen(DialConfirmScreen(number), confirmed)

    @on(Button.Pressed, "#calls-answer")
    def answer(self) -> None:
        call_id = ringing_call(self.snapshot)
        if call_id is not None:
            self._act(lambda: self._client.answer_call(call_id), _("Answered"))

    @on(Button.Pressed, "#calls-hold")
    def hold(self) -> None:
        target = hold_target(self.snapshot)
        if target is None:
            return
        done = _("Resumed") if target.state == "held" else _("On hold")
        self._act(self._client.swap_calls, done)

    @on(Button.Pressed, "#calls-hangup")
    def hangup(self) -> None:
        target = hangup_target(self.snapshot)
        if target is None:
            return
        call_id = target.call_id
        done = _("Declined") if target.ringing else _("Hung up")
        self._act(lambda: self._client.hangup_call(call_id), done)

    @on(Button.Pressed, "#calls-tones-send")
    @on(Input.Submitted, "#calls-tones")
    def send_tones(self) -> None:
        field = self.query_one("#calls-tones", Input)
        selected = tones(field.value)
        target = active_call(self.snapshot)
        if not selected or target is None:
            return
        call_id = target.call_id
        field.value = ""
        self._act(
            lambda: self._client.send_call_tones(call_id, selected),
            _("Sent tones {tones}").format(tones=selected),
        )

    # ---- contact suggestions -------------------------------------------

    @on(Input.Changed, "#calls-number")
    def number_changed(self, event: Input.Changed) -> None:
        self._search_timer.stop()
        self._contacts.begin_contact_search("")
        filled, self._filled = self._filled, None
        self._render_suggestions()
        if event.value != filled:
            self._search_timer.start(_SEARCH_DELAY_MS)

    def _search_contacts(self) -> None:
        text = self.query_one("#calls-number", Input).value.strip()
        request = self._contacts.begin_contact_search(text if len(text) >= 2 else "")
        if request is None:
            self._render_suggestions()
            return

        def found(matches: object) -> None:
            results = [
                (str(name), str(address))
                for name, address in (matches if isinstance(matches, list) else [])
            ]
            if self._contacts.apply_contact_results(request, results):
                self._render_suggestions()

        self._search_runner.submit(
            lambda: self._client.find_contacts(request.query), found, lambda _m: None,
        )

    def _render_suggestions(self) -> None:
        options = self.query_one("#calls-suggestions", OptionList)
        rows = phone_link_view.dial_suggestions(self._contacts.contact_result_rows())
        options.clear_options()
        for row in rows:
            label = Text(_plain(row["name"]))
            label.append(f"  {_plain(row['address'])}", style="dim")
            options.add_option(Option(label))
        self._suggestions = [row["address"] for row in rows]
        options.display = bool(rows)

    @on(OptionList.OptionSelected, "#calls-suggestions")
    def suggestion_selected(self, event: OptionList.OptionSelected) -> None:
        addresses = self._suggestions
        if not 0 <= event.option_index < len(addresses):
            return
        number = phone_link_view.dialable(addresses[event.option_index])
        field = self.query_one("#calls-number", Input)
        self._filled = number
        field.value = number
        field.cursor_position = len(number)
        field.focus()

    # ---- closing --------------------------------------------------------

    @on(Button.Pressed, "#calls-close")
    def close_button(self) -> None:
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)
