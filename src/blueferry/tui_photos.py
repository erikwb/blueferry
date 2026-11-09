"""Photos from a photo plugin in the terminal client (capability ``photos``).

A terminal shows no thumbnails, so :class:`PhotosScreen` lists the newest
items of the enabled photo plugin (for example Immich) by date and type,
on the shared :class:`blueferry.photos_model.PhotosTabModel` like the Qt
and GTK Photos tabs. Enter downloads the original and opens it with the
desktop's default viewer; ``c`` copies the downloaded file's path; ``f``
switches All/Photos/Videos. The list is loaded only while the screen is
open. Plugin calls run on worker threads; every label is plain text.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import unquote, urlparse

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, OptionList, Static
from textual.widgets.option_list import Option

from blueferry.i18n import _
from blueferry.photos_model import PhotosTabModel
from blueferry.text_safety import terminal_text
from blueferry.tui_plugins import open_uri
from blueferry.tui_tasks import TextualTaskRunner

# (on_change, open_file) -> model; tests pass fakes.
PhotosFactory = Callable[[Callable[[], None], Callable[[object], None]], PhotosTabModel]
FILTERS = ("all", "photos", "videos")


def _plain(value: object) -> str:
    return terminal_text(value).replace("\n", " ")


def file_url(path: object) -> str:
    """The model's ``url`` seam: a local path as a file URL, "" for none."""
    return Path(str(path)).resolve().as_uri() if path else ""


def url_path(url: object) -> str:
    """The local path of a file URL from :func:`file_url`, "" for none."""
    parsed = urlparse(str(url or ""))
    return unquote(parsed.path) if parsed.scheme == "file" else ""


def filter_label(key: str) -> str:
    return {"all": _("All"), "photos": _("Photos"), "videos": _("Videos")}[key]


def shown_items(items: list[Mapping[str, Any]], key: str) -> list[Mapping[str, Any]]:
    if key == "all":
        return list(items)
    return [item for item in items if bool(item.get("video")) == (key == "videos")]


class _ReportingRunner:
    """A runner whose failures without a handler become a notice."""

    def __init__(self, node: Any, group: str) -> None:
        self._node = node
        self._runner = TextualTaskRunner(node, group=group)

    def submit(self, operation, on_done=None, on_error=None, on_finished=None) -> None:
        def failed(message: str) -> None:
            self._node.notify(_plain(message or _("The plugin did not answer.")),
                              severity="error", markup=False)

        self._runner.submit(operation, on_done, on_error or failed, on_finished)


class PhotosScreen(ModalScreen[None]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", _("Close"), show=False),
        Binding("c", "copy_path", _("Copy path"), show=False),
        Binding("f", "next_filter", _("All/Photos/Videos"), show=False),
        Binding("r", "reload", _("Refresh"), show=False),
    ]

    def __init__(
        self,
        model: PhotosFactory | None = None,
        *,
        opener: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__()
        self._factory = model
        self._opener = opener
        self.model: PhotosTabModel | None = None
        self.filter = "all"
        self.items: list[Mapping[str, Any]] = []
        self._shown = False

    def compose(self) -> ComposeResult:
        with Vertical(id="photos-dialog", classes="dialog"):
            yield Static(_("Photos"), classes="dialog-title")
            yield Static("", id="photos-filter", classes="options-caption")
            yield Static(Text(_("Loading…"), style="dim"), id="photos-hint")
            yield OptionList(id="photos-list")
            yield Static(Text(_(
                "Enter open · c copy path · f All/Photos/Videos · r refresh · Esc close"
            ), style="dim"), classes="options-caption")
            with Horizontal(classes="dialog-actions"):
                yield Button(_("Close"), id="photos-close")

    def on_mount(self) -> None:
        self._shown = True
        if self._factory is None:
            def make(on_change, open_file) -> PhotosTabModel:
                return PhotosTabModel(
                    runner=TextualTaskRunner(self, group="photos"),
                    open_runner=_ReportingRunner(self, "photos-open"),
                    url=file_url, open_file=open_file, on_change=on_change,
                    on_error=self._photo_failed)

            self._factory = make
        self.model = self._factory(self._redraw, self._open_file)
        self._redraw()
        self.model.watch(True)

    def on_unmount(self) -> None:
        self._shown = False
        if self.model is not None:
            self.model.watch(False)

    def _photo_failed(self, message: str) -> None:
        # The model hands download failures to on_error (its own callback
        # to the runner suppresses _ReportingRunner's fallback), so the
        # screen must surface them itself, sanitized like every label.
        self.notify(_plain(message or _("The plugin did not answer.")),
                    severity="error", markup=False)

    def _open_file(self, path: object) -> None:
        uri = file_url(path)
        if not uri or not self._shown:
            return
        self.notify(_plain(_("Opening {name}").format(name=Path(str(path)).name)), markup=False)
        if self._opener is not None:
            self._opener(uri)
        else:
            open_uri(self, uri)

    def _redraw(self) -> None:
        if self.model is None or not self._shown:
            return
        state = self.model.state
        hint = self.query_one("#photos-hint", Static)
        if not state.get("loaded"):
            hint.update(Text(_("Loading…"), style="dim"))
        else:
            hint.update(Text(_plain(state.get("hint")), style="dim"))
        hint.display = not state.get("loaded") or bool(state.get("hint"))
        self.query_one("#photos-filter", Static).update(Text(" · ".join(
            f"[{filter_label(key)}]" if key == self.filter else filter_label(key)
            for key in FILTERS)))
        self.items = shown_items(list(state.get("items") or []), self.filter)
        listing = self.query_one("#photos-list", OptionList)
        highlighted = listing.highlighted
        listing.clear_options()
        listing.add_options([
            Option(Text(f"{_plain(item.get('label'))}{'  ✓' if item.get('original') else ''}"))
            for item in self.items
        ])
        listing.display = bool(self.items)
        if self.items:
            listing.highlighted = (highlighted if highlighted is not None
                                   and highlighted < len(self.items) else 0)

    def _selected(self) -> Mapping[str, Any] | None:
        index = self.query_one("#photos-list", OptionList).highlighted
        return self.items[index] if index is not None and 0 <= index < len(self.items) else None

    @on(OptionList.OptionSelected, "#photos-list")
    def _open_selected(self) -> None:
        item = self._selected()
        if item is not None and self.model is not None:
            self.model.open(str(item["id"]))

    def action_copy_path(self) -> None:
        item = self._selected()
        path = url_path(item.get("original")) if item is not None else ""
        if not path:
            self.notify(_("Open the photo first; then its file can be copied."),
                        severity="warning")
            return
        self.app.copy_to_clipboard(path)
        self.notify(_plain(_("Copied {path}").format(path=path)), markup=False)

    def action_next_filter(self) -> None:
        self.filter = FILTERS[(FILTERS.index(self.filter) + 1) % len(FILTERS)]
        self._redraw()

    def action_reload(self) -> None:
        if self.model is not None:
            self.model.refresh()

    @on(Button.Pressed, "#photos-close")
    def _close_button(self) -> None:
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)
