"""The "Tools" section of the GTK client: UxPlay screen mirroring, LocalSend
and the iPhone's camera roll over USB.

GTK counterpart of the Qt client's ``CompanionToolsSection.qml``, on the
shared :class:`blueferry.companion_model.CompanionToolsModel`. These are
local programs this client starts, not daemon features. Tools that are not
installed stay visible but greyed out, with what to install. USB devices
come and go without a signal, so the section looks again every few seconds,
but only while someone can see it (:meth:`CompanionToolsSection.set_watched`:
shown, and the window active), because each look runs ``idevice_id``. Tool
output can carry anything: every text is plain.
"""

from __future__ import annotations

from collections.abc import Callable

from gi.repository import Adw, Gtk

from blueferry.client_tasks import TimerFactory
from blueferry.companion_model import CompanionToolsModel
from blueferry.i18n import _
from blueferry.ui.util import glib_timer

POLL_MS = 5000
ToolsFactory = Callable[[Callable[[], None]], CompanionToolsModel]
_ICONS = {"mirror": "video-display-symbolic", "send": "document-send-symbolic"}


def default_tools(client) -> ToolsFactory:
    def make(on_change: Callable[[], None]) -> CompanionToolsModel:
        return CompanionToolsModel(runner=client.task_runner("tools"), on_change=on_change)

    return make


def tool_icon(tool: dict) -> str:
    if tool.get("key") == "mirror" and tool.get("active"):
        return "media-playback-stop-symbolic"
    return _ICONS.get(str(tool.get("key")), "folder-pictures-symbolic")


class CompanionToolsSection(Gtk.Box):
    def __init__(self, tools: ToolsFactory, *, timer: TimerFactory | None = None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._watched = False
        self._poll = (timer or glib_timer)(self._tick)
        self.rows: dict[str, tuple[Adw.ActionRow, Gtk.Spinner, Gtk.Button | None]] = {}
        self._icons: dict[str, Gtk.Image] = {}
        self._keys: tuple = ()

        self.append(Gtk.Label(label=_("Tools"), xalign=0, css_classes=["heading"]))
        self.list = Gtk.ListBox(
            selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"],
        )
        self.append(self.list)

        # ifuse and idevicepair output: plain text.
        self.message_row = Adw.ActionRow(use_markup=False, title_lines=0, visible=False)
        self.message_icon = Gtk.Image()
        self.message_row.add_prefix(self.message_icon)
        self.pair_button = Gtk.Button(
            label=_("Ask the iPhone again"), valign=Gtk.Align.CENTER, visible=False,
        )
        self.pair_button.connect("clicked", lambda _b: self.model.run("pair"))
        self.message_row.add_suffix(self.pair_button)
        close = Gtk.Button(
            icon_name="window-close-symbolic", tooltip_text=_("Dismiss"),
            valign=Gtk.Align.CENTER, css_classes=["flat"],
        )
        close.update_property([Gtk.AccessibleProperty.LABEL], [_("Dismiss")])
        close.connect("clicked", lambda _b: self.model.clear_message())
        self.message_row.add_suffix(close)

        self.model = tools(self._render)
        self._render()

    # ---- polling ------------------------------------------------------------

    @property
    def watched(self) -> bool:
        return self._watched

    def set_watched(self, watched: bool) -> None:
        """Look for tools and a USB iPhone only while someone can see it."""
        if watched == self._watched:
            return
        self._watched = watched
        if watched:
            self.model.refresh()
            self._poll.start(POLL_MS)
        else:
            self._poll.stop()

    def _tick(self) -> None:
        if not self._watched:
            return
        if not self.model.busy:
            self.model.refresh()
        self._poll.start(POLL_MS)

    # ---- rendering ----------------------------------------------------------------

    def _render(self) -> None:
        state = self.model.state()
        tools = {tool["key"]: tool for tool in state["tools"]}
        rows = [tool for tool in state["tools"] if tool["key"] != "eject"]
        keys = tuple(tool["key"] for tool in rows)
        if keys != self._keys:
            self._keys = keys
            self._build(rows)
        busy = state["busy"]
        eject = tools.get("eject", {})
        for key, (row, spinner, eject_button) in self.rows.items():
            tool = tools.get(key, {})
            row.set_title(tool.get("title") or "")
            row.set_subtitle(tool.get("subtitle") or "")
            row.set_tooltip_text(None if tool.get("installed") else tool.get("subtitle") or None)
            row.set_sensitive(tool.get("enabled") is True and not busy)
            self._icons[key].set_from_icon_name(tool_icon(tool))
            spinner.set_visible(busy == key)
            spinner.set_spinning(busy == key)
            if eject_button is not None:
                ejectable = eject.get("enabled") is True
                eject_button.set_visible(ejectable)
                eject_button.set_sensitive(not busy)
                eject_button.set_tooltip_text(eject.get("title") or _("Eject"))
        message = state["message"]
        self.message_row.set_visible(bool(message))
        self.message_row.set_title(message)
        ok = state["messageOk"] is not False
        self.message_icon.set_from_icon_name(
            "object-select-symbolic" if ok else "dialog-warning-symbolic")
        for name in ("success", "warning"):
            self.message_icon.remove_css_class(name)
        self.message_icon.add_css_class("success" if ok else "warning")
        self.pair_button.set_visible(state["needsPairing"] is True)
        self.pair_button.set_sensitive(not busy)

    def _build(self, tools: list[dict]) -> None:
        child = self.list.get_first_child()
        while child is not None:
            self.list.remove(child)
            child = self.list.get_first_child()
        self.rows, self._icons = {}, {}
        for tool in tools:
            key = tool["key"]
            row = Adw.ActionRow(use_markup=False, subtitle_lines=2, activatable=True)
            icon = Gtk.Image(icon_name=tool_icon(tool))
            row.add_prefix(icon)
            row.connect("activated", lambda _r, action=key: self.model.run(action))
            spinner = Gtk.Spinner(valign=Gtk.Align.CENTER, visible=False)
            row.add_suffix(spinner)
            eject = None
            if key == "photos":
                eject = Gtk.Button(
                    icon_name="media-eject-symbolic", valign=Gtk.Align.CENTER,
                    css_classes=["flat"], visible=False,
                )
                eject.update_property([Gtk.AccessibleProperty.LABEL], [_("Eject")])
                eject.connect("clicked", lambda _b: self.model.run("eject"))
                row.add_suffix(eject)
            self.rows[key] = (row, spinner, eject)
            self._icons[key] = icon
            self.list.append(row)
        self.list.append(self.message_row)
