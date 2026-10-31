"""Plugin surfaces in the terminal client (PLUGINS.md, ApiVersion 1.2 to 1.4).

- :class:`PluginCardsPanel` is the "From Plugins" section of the iPhone
  overview, on the shared
  :class:`blueferry.plugin_surfaces_model.PluginSurfacesModel` like the Qt
  and GTK cards: every enabled card plugin's items as headings with their
  actions below; Enter runs the highlighted action. A plugin that failed
  shows a dimmed line instead of its items.
- :class:`SendToScreen` is "Send to…": pick a target of a share plugin and
  type file paths (→ takes the path completion). A card action with
  ``sendTo`` (ApiVersion 1.4, for example "Send files…" on a LocalSend
  device) opens it with only that target.
- ``Plugin1.CardChanged`` is heard only from the owners of the enabled
  card plugins' bus names
  (:class:`blueferry.plugin_card_watch.CardChangeWatcher`), so no other
  session-bus client can make the overview call every plugin in a loop.

The panel loads only while the overview is open; closing it stops the
watcher and a pending reload. Plugin calls run on worker threads with the
core's timeouts. Plugin text is rendered as Rich ``Text`` after
``terminal_text``, so it carries neither markup nor terminal controls.
"""
from __future__ import annotations

import asyncio
import os
import shlex
from collections.abc import Callable, Mapping, Sequence
from typing import Any, ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.suggester import Suggester
from textual.widgets import Button, Input, OptionList, Static
from textual.widgets.option_list import Option

from blueferry.i18n import _
from blueferry.plugin_card_watch import CardChangeWatcher
from blueferry.plugin_surfaces_model import PluginSurfacesModel
from blueferry.text_safety import terminal_text
from blueferry.tui_tasks import TextualTaskRunner, timer_factory

# (on_change, opener, watch) -> model; tests pass fakes.
SurfacesFactory = Callable[
    [Callable[[], None], Callable[[str], None], Callable[[list[str]], None]],
    PluginSurfacesModel,
]
# At most this many entries are offered for one completion.
_MAX_COMPLETION_ENTRIES = 2000


def _plain(value: object) -> str:
    return terminal_text(value).replace("\n", " ")


# ---- the card rows ------------------------------------------------------------


def card_options(cards: Sequence[Mapping[str, Any]]) -> tuple[list[Option], list[tuple]]:
    """Option list lines for the model's ``cards`` rows and what each
    selectable line runs.

    Plugin names (with more than one plugin) and items are disabled
    headings; their actions are the selectable lines. Returns the options
    and, per option with an id ``action:<n>``, ``(plugin id, item row,
    action row)`` at index ``n``.
    """
    options: list[Option] = []
    actions: list[tuple] = []
    for card in cards:
        ok = card.get("ok") is True
        if len(cards) > 1 or not ok:
            options.append(Option(Text(_plain(card.get("name")), style="bold #7dd3fc"),
                                  disabled=True))
        if not ok:
            options.append(Option(Text(f"  {_plain(card.get('hint'))}", style="dim"),
                                  disabled=True))
            continue
        for item in card.get("items") or []:
            line = Text(f"  {_plain(item.get('title'))}", style="bold")
            if item.get("subtitle"):
                line.append(f"  {_plain(item.get('subtitle'))}", style="dim")
            options.append(Option(line, disabled=True))
            for action in item.get("actions") or []:
                marker = "▸" if action.get("primary") else "·"
                label = _plain(action.get("labelPlain"))
                options.append(Option(Text(f"    {marker} {label}"),
                                      id=f"action:{len(actions)}"))
                actions.append((str(card.get("pluginId") or ""), item, action))
    return options, actions


# ---- opening what a plugin hands over ---------------------------------------------


def open_uri(node: Any, uri: str) -> None:
    """Open a URI the shared model checked, with the desktop's default
    handler, off the UI thread; a terminal without a desktop gets a notice."""
    from blueferry import desktop_open

    def failed(message: str) -> None:
        node.notify(
            _plain(_("Could not open {uri}: {reason}").format(uri=uri, reason=message)),
            severity="error", markup=False,
        )

    TextualTaskRunner(node, group="open-uri").submit(
        lambda: desktop_open.open_uri(uri), None, failed,
    )


# ---- the panel ------------------------------------------------------------------


def default_surfaces(node: Any) -> SurfacesFactory:
    def make(on_change, opener, watch) -> PluginSurfacesModel:
        return PluginSurfacesModel(
            runner=TextualTaskRunner(node, group="plugin-surfaces"),
            timer=timer_factory(node), opener=opener, on_change=on_change, watch=watch,
        )

    return make


class PluginCardsPanel(Vertical):
    """"From Plugins" with "Send to…"; hidden while there is nothing to show."""

    def __init__(
        self,
        surfaces: SurfacesFactory | None = None,
        *,
        watcher_bus: Callable[[], Any] | None = None,
        opener: Callable[[str], None] | None = None,
        id: str | None = "plugin-cards",
    ) -> None:
        super().__init__(id=id)
        self._surfaces = surfaces
        self._opener = opener
        self.watcher = (CardChangeWatcher(self._card_changed, watcher_bus) if watcher_bus
                        else CardChangeWatcher(self._card_changed))
        self.model: PluginSurfacesModel | None = None
        self.card_actions: list[tuple] = []
        self._cards_key: object = None
        self._stopped = False

    def compose(self) -> ComposeResult:
        yield Static(_("From Plugins"), classes="plugin-cards-title")
        yield OptionList(id="plugin-cards-list")
        yield Static("", id="plugin-cards-message", classes="plugin-cards-message")
        with Horizontal(id="plugin-cards-buttons"):
            yield Button(_("Send to…"), id="plugin-cards-send")
            yield Button(_("Refresh"), id="plugin-cards-refresh")

    def on_mount(self) -> None:
        self.display = False
        factory = self._surfaces or default_surfaces(self)
        self.model = factory(self._redraw, self._open, self._watch)
        self._redraw()
        self.model.reload()

    def on_unmount(self) -> None:
        self.stop()

    def stop(self) -> None:
        """The overview closes: no more signals, no pending reload."""
        self._stopped = True
        self.watcher.stop()
        if self.model is not None:
            self.model.stop()

    def _watch(self, names: list[str]) -> None:
        # A load that ends after the overview closed subscribes nothing.
        if not self._stopped:
            self.watcher.watch(names)

    def _card_changed(self) -> None:
        if not self._stopped and self.model is not None:
            self.model.schedule_reload()

    def _open(self, uri: str) -> None:
        # Only what the model hands over after its plugin re-check.
        if self._stopped:
            return
        if self._opener is not None:
            self._opener(uri)
        else:
            open_uri(self, uri)

    # ---- rendering --------------------------------------------------------------

    @property
    def can_share(self) -> bool:
        state = self.model.state() if self.model is not None else {}
        return state.get("shareAvailable") is True or bool(state.get("targets"))

    def _redraw(self) -> None:
        if self.model is None or not self.is_attached:
            return
        state = self.model.state()
        busy = bool(state["busy"])
        cards = state["cards"]
        key = repr(cards)
        if key != self._cards_key:
            self._cards_key = key
            options, self.card_actions = card_options(cards)
            listing = self.query_one("#plugin-cards-list", OptionList)
            highlighted = listing.highlighted
            listing.clear_options()
            listing.add_options(options)
            listing.display = bool(options)
            if highlighted is not None and highlighted < len(options):
                listing.highlighted = highlighted
        message = _plain(state["message"])
        line = self.query_one("#plugin-cards-message", Static)
        line.update(Text(message, style="#86efac" if state["messageOk"] is not False
                         else "#fca5a5"))
        line.display = bool(message)
        send = self.query_one("#plugin-cards-send", Button)
        send.display = self.can_share
        send.disabled = busy
        self.query_one("#plugin-cards-refresh", Button).disabled = busy
        self.display = bool(cards) or self.can_share or bool(message)
        screen = self.app.screen if self.app is not None else None
        if isinstance(screen, SendToScreen) and screen.model is self.model:
            screen.model_changed()

    # ---- actions ------------------------------------------------------------------

    @on(OptionList.OptionSelected, "#plugin-cards-list")
    def _selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        ident = event.option.id or ""
        if not ident.startswith("action:"):
            return
        index = int(ident.partition(":")[2])
        if 0 <= index < len(self.card_actions):
            self.run_card_action(*self.card_actions[index])

    def run_card_action(self, plugin_id: str, item: Mapping[str, Any],
                   action: Mapping[str, Any]) -> None:
        """Send files (``sendTo``) or let the plugin act."""
        if self.model is None or self.model.state()["busy"]:
            return
        target = str(action.get("sendTo") or "")
        if target:
            self.app.push_screen(SendToScreen(
                self.model, card_target=(plugin_id, target, str(item.get("title") or "")),
            ))
            return
        self.model.invoke(plugin_id, str(item.get("id") or ""), str(action.get("id") or ""))

    @on(Button.Pressed, "#plugin-cards-send")
    def _send_button(self, event: Button.Pressed) -> None:
        event.stop()
        if self.model is not None:
            self.app.push_screen(SendToScreen(self.model))

    @on(Button.Pressed, "#plugin-cards-refresh")
    def _refresh_button(self, event: Button.Pressed) -> None:
        event.stop()
        if self.model is not None:
            self.model.clear_message()
            self.model.reload()


# ---- Send to… ---------------------------------------------------------------------


def split_paths(text: str) -> list[str]:
    """File paths typed like a shell argument list (quotes, ``\\ ``, ``~``).

    Raises ``ValueError`` for an unclosed quote.
    """
    return [os.path.expanduser(part) for part in shlex.split(text)]


def _last_word(value: str) -> str | None:
    """The last shell word of ``value`` as typed, or None inside a quote.

    Spaces escaped with a backslash or inside quotes do not split words,
    so ``…/Photo\\ on`` is one word (which :func:`complete_path` then
    leaves alone) rather than ``on``.
    """
    start, quote, index = 0, "", 0
    while index < len(value):
        char = value[index]
        if quote:
            if char == "\\" and quote == '"':
                index += 1  # \" stays inside double quotes
            elif char == quote:
                quote = ""
        elif char == "\\":
            index += 1
        elif char in "'\"":
            quote = char
        elif char.isspace():
            start = index + 1
        index += 1
    return None if quote else value[start:]


def complete_path(value: str, *, limit: int = _MAX_COMPLETION_ENTRIES) -> str | None:
    """``value`` with its last path completed as far as it is unambiguous.

    Only a last word without quotes or backslashes is completed; a space
    in the completion is escaped so :func:`split_paths` reads it back.
    *Blocking* (lists one directory).
    """
    word = _last_word(value)
    if not word or any(char in word for char in "'\"\\"):
        return None
    expanded = os.path.expanduser(word)
    directory, prefix = os.path.split(expanded)
    try:
        with os.scandir(directory or ".") as entries:
            names: list[tuple[str, bool]] = []
            for entry in entries:
                if len(names) >= limit:
                    break
                if entry.name.startswith(prefix) and (
                        prefix.startswith(".") or not entry.name.startswith(".")):
                    names.append((entry.name, entry.is_dir()))
    except OSError:
        return None
    if not names:
        return None
    # A common prefix of names in one directory, not of whole paths.
    common = os.path.commonprefix([name for name, _is_dir in names])  # noqa: RUF071
    rest = common[len(prefix):]
    if len(names) == 1 and names[0][1]:
        rest += "/"
    if not rest:
        return None
    return value + rest.replace(" ", "\\ ")


class PathSuggester(Suggester):
    """Textual's inline suggestion for :class:`SendToScreen`'s path field."""

    def __init__(self) -> None:
        super().__init__(use_cache=False, case_sensitive=True)

    async def get_suggestion(self, value: str) -> str | None:
        return await asyncio.to_thread(complete_path, value)


class SendToScreen(ModalScreen[None]):
    """Pick a target, type paths, Enter sends; closes once the files went."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", _("Close"), show=False),
    ]

    def __init__(
        self,
        model: PluginSurfacesModel,
        *,
        card_target: tuple[str, str, str] | None = None,
    ) -> None:
        super().__init__()
        self.model = model
        # (plugin id, target id, item title) of a card action with sendTo.
        self.card_target = card_target
        self.sending = False
        self._targets_key: object = None
        self._keys: list[str] = []

    def compose(self) -> ComposeResult:
        if self.card_target is None:
            title = _("Send to…")
        else:
            title = _("Send Files to {target}").format(target=_plain(self.card_target[2]))
        with Vertical(id="send-dialog", classes="dialog"):
            yield Static(Text(title), classes="dialog-title")
            if self.card_target is None:
                yield Static(_("Target"), classes="field-label")
                yield OptionList(id="send-targets")
            yield Static(_("Files"), classes="field-label")
            yield Input(
                placeholder=_("~/Pictures/a.jpg \"/path with spaces/b.pdf\""),
                suggester=PathSuggester(), id="send-paths",
            )
            yield Static(Text(_("→ completes a path; Enter sends.")),
                         classes="options-caption")
            yield Static("", id="send-status")
            with Horizontal(classes="dialog-actions"):
                yield Button(_("Close"), id="send-close")
                yield Button(_("Send"), variant="primary", id="send-go")

    def on_mount(self) -> None:
        if self.card_target is None:
            self.model.load_targets()
        self.model_changed()
        self.query_one("#send-paths", Input).focus()

    def model_changed(self) -> None:
        """The panel forwards every change of the shared model."""
        if not self.is_attached:
            return
        state = self.model.state()
        busy = bool(state["busy"])
        if self.card_target is None:
            self._render_targets(state)
        status = self.query_one("#send-status", Static)
        if busy and self.sending:
            status.update(Text(_("Sending…"), style="dim"))
        elif self.sending:
            self.sending = False
            message = _plain(state["message"])
            if state["messageOk"] is not False:
                self.dismiss(None)
                if message:
                    self.app.notify(message, markup=False)
                return
            status.update(Text(message, style="#fca5a5"))
        self.query_one("#send-go", Button).disabled = busy

    def _render_targets(self, state: Mapping[str, Any]) -> None:
        targets = state["targets"]
        key = (repr(targets), state["targetsLoading"], repr(state["targetProblems"]))
        if key == self._targets_key:
            return
        self._targets_key = key
        listing = self.query_one("#send-targets", OptionList)
        highlighted = listing.highlighted
        listing.clear_options()
        self._keys = [str(target["key"]) for target in targets]
        listing.add_options([Option(Text(_plain(target["label"]))) for target in targets])
        if not targets:
            text = (_("Looking for targets…") if state["targetsLoading"]
                    else _("No targets right now"))
            listing.add_option(Option(Text(text, style="dim"), disabled=True))
        for problem in state["targetProblems"]:
            listing.add_option(Option(Text(_plain(problem), style="dim"), disabled=True))
        if targets:
            listing.highlighted = (highlighted if highlighted is not None
                                   and highlighted < len(targets) else 0)

    @on(Input.Submitted, "#send-paths")
    @on(Button.Pressed, "#send-go")
    def send(self) -> None:
        if self.model.state()["busy"]:
            return
        status = self.query_one("#send-status", Static)
        try:
            paths = split_paths(self.query_one("#send-paths", Input).value)
        except ValueError:
            status.update(Text(_("A quote is not closed."), style="#fca5a5"))
            return
        if not paths:
            status.update(Text(_("Type the path of at least one file."), style="#fca5a5"))
            return
        if self.card_target is not None:
            plugin_id, target, label = self.card_target
            self.sending = True
            self.model.send_from_card(plugin_id, target, label, paths)
            return
        index = self.query_one("#send-targets", OptionList).highlighted
        if index is None or not 0 <= index < len(self._keys):
            status.update(Text(_("Choose a target first."), style="#fca5a5"))
            return
        self.sending = True
        self.model.send(self._keys[index], paths)

    @on(Button.Pressed, "#send-close")
    def close_button(self) -> None:
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)
