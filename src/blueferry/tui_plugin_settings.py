"""Settings > Plugins in the terminal client.

:class:`PluginsPane` is the Plugins tab of the Settings screen, on the
shared :class:`blueferry.plugin_settings_model.PluginSettingsModel` like the
Qt and GTK pages, with the GTK page's presentation rules
(:mod:`blueferry.plugin_settings_presenter`):

- Installed plugins with their state. On the highlighted one: Space turns
  it on or off (the kill switch), Enter opens its settings, ``u`` checks
  for an update, Delete removes it (after a question) and ``g`` shows its
  log in the terminal. The model's notes, such as a plugin that keeps
  running until the service restarts, appear in the message line.
- The plugins of the configured plugin lists; Enter installs or updates
  one. Every install or update first shows what it will run
  (:class:`InstallConfirmScreen`), as in the other clients.
- An own https Git URL, and the plugin lists themselves.

:class:`PluginConfigScreen` is the guided settings form: sections that
fold, ShowIf, notes, help links, the client's pre-check on every edit,
"Test Connection", the browser sign-in and the plugin's own buttons. A
stored secret is never shown; its field stays empty and masked, and empty
keeps it. Everything that blocks runs on one worker; plugin and index text
is rendered as Rich ``Text`` after ``terminal_text``.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any, ClassVar

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, Checkbox, Input, OptionList, Select, Static
from textual.widgets.option_list import Option

from blueferry import plugin_logs
from blueferry import plugin_settings_presenter as present
from blueferry.i18n import _
from blueferry.plugin_settings_model import PluginSettingsModel
from blueferry.text_safety import terminal_text
from blueferry.tui_plugins import open_uri
from blueferry.tui_tasks import SerialTaskRunner, TextualTaskRunner, timer_factory

# (on_change, opener) -> model; tests pass fakes.
ModelFactory = Callable[[Callable[[], None], Callable[[str], None]], PluginSettingsModel]
LogReader = Callable[[str], tuple[Any, str]]
_OK = "#86efac"
_ERROR = "#fca5a5"


def _plain(value: object) -> str:
    return terminal_text(value).replace("\n", " ")


def _lines(value: object) -> str:
    return terminal_text(value)


def default_model(node: Widget) -> ModelFactory:
    def make(on_change: Callable[[], None], opener: Callable[[str], None]) -> PluginSettingsModel:
        return PluginSettingsModel(
            runner=SerialTaskRunner(node, group="plugin-settings"),
            timer=timer_factory(node), opener=opener, on_change=on_change,
        )

    return make


def plugin_line(plugin: Mapping[str, Any]) -> Text:
    """One installed plugin: on/off, name and version, state, capabilities."""
    mark = "●" if plugin.get("enabled") else "○"
    text = Text(f"{mark} ", style=_OK if plugin.get("enabled") else "dim")
    text.append(_plain(present.plugin_title(plugin)), style="bold")
    subtitle = present.plugin_subtitle(plugin)
    if subtitle:
        text.append(f"  {_plain(subtitle)}", style="dim")
    if plugin.get("detail"):
        text.append(f"\n    {_plain(plugin['detail'])}", style="dim")
    return text


def store_line(card: Mapping[str, Any]) -> Text:
    """One plugin of a plugin list with what Enter would do."""
    emoji = _plain(card.get("emoji"))
    text = Text(f"{emoji} " if emoji else "")
    text.append(_plain(card.get("name")), style="bold")
    text.append(f"  [{_plain(card.get('stateText'))}]",
                style="#7dd3fc" if card.get("installable") else "dim")
    for line in present.store_subtitle(card).splitlines():
        text.append(f"\n    {_plain(line)}", style="dim")
    return text


class PluginsPane(Vertical):
    """The Plugins tab; loads when it is first shown."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("space", "toggle_plugin", _("On/Off"), show=False),
        Binding("u", "update_plugin", _("Update"), show=False),
        Binding("delete", "remove_selected", _("Remove"), show=False),
        Binding("g", "show_log", _("Show Log"), show=False),
    ]

    def __init__(
        self,
        model: ModelFactory | None = None,
        *,
        opener: Callable[[str], None] | None = None,
        read_log: LogReader = plugin_logs.tail,
        id: str | None = "plugins-pane",
    ) -> None:
        super().__init__(id=id)
        self._factory = model
        self._opener = opener
        self._read_log = read_log
        self.model: PluginSettingsModel | None = None
        self._loaded_once = False
        self._plugins_key: object = None
        self._store_key: object = None
        self._indexes_key: object = None
        self.plugin_ids: list[str] = []
        self.store_ids: list[str] = []
        self.index_urls: list[str] = []
        self._confirming = False
        self.form: PluginConfigScreen | None = None
        self._shown = False  # composed and mounted; widgets exist

    def compose(self) -> ComposeResult:
        yield Static("", id="plugins-message")
        yield Static(_("Installed"), classes="options-section")
        yield OptionList(id="plugins-installed")
        yield Static(Text(_(
            "Space on/off · Enter settings · u update · Delete remove · g log"
        )), classes="options-caption")
        with Horizontal(classes="options-action"):
            yield Button(_("On/Off"), id="plugins-toggle")
            yield Button(_("Settings"), id="plugins-settings")
            yield Button(_("Update"), id="plugins-update")
            yield Button(_("Remove"), id="plugins-remove")
            yield Button(_("Show Log"), id="plugins-log")
            yield Button(_("Refresh"), id="plugins-refresh")
        yield Static(_("Add Plugins"), classes="options-section")
        yield OptionList(id="plugins-store")
        with Horizontal(classes="options-action"):
            yield Static(Text(_("Enter installs or updates the highlighted plugin.")),
                         classes="options-action-text")
            yield Button(_("Reload the plugin list"), id="plugins-store-refresh")
        yield Static(_("Own URL"), classes="options-section")
        yield Input(placeholder=_("Git URL (https://github.com/…/blueferry-plugin-…)"),
                    id="plugins-url")
        yield Input(placeholder=_("Tag or commit (empty: the newest version)"),
                    id="plugins-ref")
        with Horizontal(classes="options-action"):
            yield Static(Text(_("Shows what the plugin installs and asks first")),
                         classes="options-action-text")
            yield Button(_("Check and Install…"), id="plugins-install-url")
        yield Static(_("Plugin Lists"), classes="options-section")
        yield OptionList(id="plugins-indexes")
        yield Input(placeholder=_("Add a list (https://… plugins-index.json)"),
                    id="plugins-index-url")
        with Horizontal(classes="options-action"):
            yield Static(Text(_("Delete removes the highlighted list.")),
                         classes="options-action-text")
            yield Button(_("Add List"), id="plugins-add-index")
            yield Button(_("Default"), id="plugins-default-index")

    def on_mount(self) -> None:
        factory = self._factory or default_model(self)
        self._shown = True
        self.model = factory(self._redraw, self._open)
        self._redraw()

    def on_show(self, _event: events.Show) -> None:
        # Status reads start the enabled plugins: only once the tab is seen.
        if not self._loaded_once and self.model is not None:
            self._loaded_once = True
            self.model.load()
            self.model.load_store(False)

    def on_unmount(self) -> None:
        self._shown = False  # the widgets are going away
        if self.model is None:
            return
        if self.model.state()["config"]:
            self.model.close_config()
        if self.model.state()["pending"]:
            self.model.cancel_install()

    def _open(self, uri: str) -> None:
        if self._opener is not None:
            self._opener(uri)
        else:
            open_uri(self, uri)

    # ---- rendering ----------------------------------------------------------

    def _redraw(self) -> None:
        if self.model is None or not self._shown:
            return
        state = self.model.state()
        busy = bool(state["busy"])
        message = _plain(state["message"])
        line = self.query_one("#plugins-message", Static)
        if busy:
            line.update(Text(_("Working…"), style="dim"))
        else:
            line.update(Text(message, style=_OK if state["messageOk"] is not False else _ERROR))
        line.display = busy or bool(message)
        self._render_plugins(state)
        self._render_store(state)
        self._render_indexes(state)
        for ident in ("plugins-toggle", "plugins-settings", "plugins-update", "plugins-remove",
                      "plugins-store-refresh", "plugins-refresh", "plugins-add-index",
                      "plugins-default-index", "plugins-install-url"):
            self.query_one(f"#{ident}", Button).disabled = busy
        self._sync_install(state["pending"])
        config = state["config"]
        if self.form is not None:
            if config.get("id") == self.form.plugin_id:
                self.form.show_config(config, busy)
            else:
                # Saved, disabled or removed: the model closed the form.
                form, self.form = self.form, None
                if form.is_attached:
                    form.dismiss(None)

    def _render_plugins(self, state: Mapping[str, Any]) -> None:
        key = json.dumps([state["plugins"], state["ignored"], state["loaded"]],
                         sort_keys=True, default=str)
        if key == self._plugins_key:
            return
        self._plugins_key = key
        listing = self.query_one("#plugins-installed", OptionList)
        highlighted = listing.highlighted
        listing.clear_options()
        self.plugin_ids = [str(plugin["id"]) for plugin in state["plugins"]]
        listing.add_options([Option(plugin_line(plugin)) for plugin in state["plugins"]])
        if not state["loaded"]:
            listing.add_option(Option(Text(_("Loading…"), style="dim"), disabled=True))
        elif not state["plugins"]:
            listing.add_option(Option(Text(
                _("No plugins installed. Add one from the list below."), style="dim"),
                disabled=True))
        for ignored in state["ignored"]:
            text = Text(_plain(_("Ignored {name}").format(name=ignored["name"])), style="dim")
            text.append(f"\n    {_plain(ignored['reason'])}", style="dim")
            listing.add_option(Option(text, disabled=True))
        if self.plugin_ids:
            listing.highlighted = (highlighted if highlighted is not None
                                   and highlighted < len(self.plugin_ids) else 0)

    def _render_store(self, state: Mapping[str, Any]) -> None:
        store = state["store"]
        key = json.dumps(store, sort_keys=True, default=str)
        if key == self._store_key:
            return
        self._store_key = key
        listing = self.query_one("#plugins-store", OptionList)
        highlighted = listing.highlighted
        listing.clear_options()
        items = store.get("items") or []
        self.store_ids = [str(card["id"]) for card in items]
        listing.add_options([Option(store_line(card)) for card in items])
        if not store.get("loaded"):
            listing.add_option(Option(Text(_("Loading…"), style="dim"), disabled=True))
        for problem in store.get("problems") or []:
            listing.add_option(Option(Text(
                f"{_plain(problem['index'])}: {_plain(problem['problem'])}", style=_ERROR),
                disabled=True))
        if self.store_ids:
            listing.highlighted = (highlighted if highlighted is not None
                                   and highlighted < len(self.store_ids) else 0)

    def _render_indexes(self, state: Mapping[str, Any]) -> None:
        key = (tuple(state["indexes"]), state["defaultIndex"])
        if key == self._indexes_key:
            return
        self._indexes_key = key
        listing = self.query_one("#plugins-indexes", OptionList)
        listing.clear_options()
        self.index_urls = [str(url) for url in state["indexes"]]
        listing.add_options([Option(Text(_plain(url))) for url in self.index_urls])

    def _sync_install(self, pending: Mapping[str, Any]) -> None:
        if pending and not self._confirming:
            self._confirming = True
            self.app.push_screen(InstallConfirmScreen(pending), self._confirmed)

    def _confirmed(self, yes: bool | None) -> None:
        self._confirming = False
        if self.model is None:
            return
        if yes:
            self.model.confirm_install()
        else:
            self.model.cancel_install()

    # ---- the highlighted entries --------------------------------------------------

    def _highlighted(self, listing_id: str, ids: list[str]) -> str:
        index = self.query_one(listing_id, OptionList).highlighted
        return ids[index] if index is not None and 0 <= index < len(ids) else ""

    def _plugin(self, plugin_id: str) -> Mapping[str, Any] | None:
        if self.model is None:
            return None
        return next((p for p in self.model.state()["plugins"] if p["id"] == plugin_id), None)

    def _busy(self) -> bool:
        return self.model is None or bool(self.model.state()["busy"])

    def _installed_focused(self) -> bool:
        # Space and Delete act on the installed list only: with the focus in
        # the store, a stray Space must not flip a plugin's kill switch.
        return self.query_one("#plugins-installed", OptionList).has_focus

    def _toggle_highlighted(self) -> None:
        plugin = self._plugin(self._highlighted("#plugins-installed", self.plugin_ids))
        if plugin is not None and self.model is not None and not self._busy():
            self.model.set_enabled(str(plugin["id"]), not plugin.get("enabled"))

    def action_toggle_plugin(self) -> None:
        # The focus guard belongs to the keyboard binding: Space in the
        # store must not flip a kill switch. The On/Off button holds the
        # focus when pressed, so it calls _toggle_highlighted directly.
        if not self._installed_focused():
            return
        self._toggle_highlighted()

    def action_update_plugin(self) -> None:
        plugin = self._plugin(self._highlighted("#plugins-installed", self.plugin_ids))
        if plugin is not None and plugin.get("managed") and self.model and not self._busy():
            self.model.prepare_update(str(plugin["id"]))

    def _remove_highlighted(self) -> None:
        plugin = self._plugin(self._highlighted("#plugins-installed", self.plugin_ids))
        if plugin is None or not plugin.get("managed") or self._busy():
            return
        plugin_id = str(plugin["id"])

        def answered(yes: bool | None) -> None:
            if yes and self.model is not None:
                self.model.remove(plugin_id)

        self.app.push_screen(RemoveConfirmScreen(str(plugin.get("name") or "")), answered)

    def action_remove_selected(self) -> None:
        if self.query_one("#plugins-indexes", OptionList).has_focus:
            url = self._highlighted("#plugins-indexes", self.index_urls)
            if url and self.model is not None and not self._busy():
                self.model.set_indexes([other for other in self.index_urls if other != url])
            return
        if not self._installed_focused():
            return
        self._remove_highlighted()

    def action_show_log(self) -> None:
        plugin_id = self._highlighted("#plugins-installed", self.plugin_ids)
        if plugin_id:
            plugin = self._plugin(plugin_id) or {}
            self.app.push_screen(PluginLogScreen(
                plugin_id, str(plugin.get("name") or plugin_id), self._read_log))

    def open_settings(self, plugin_id: str) -> None:
        plugin = self._plugin(plugin_id)
        if (plugin is None or not plugin.get("hasConfig") or not plugin.get("enabled")
                or self.model is None):
            return
        self.model.load_config(plugin_id)
        config = self.model.state()["config"]
        if config.get("id") != plugin_id:
            return
        form = self.form = PluginConfigScreen(self.model, config)

        def closed(_result: object) -> None:
            # Left by the user (not closed by the model): close the model's form.
            if self.form is form:
                self.form = None
                if self.model is not None and self.model.state()["config"].get("id") == plugin_id:
                    self.model.close_config()

        self.app.push_screen(form, closed)

    # ---- events ---------------------------------------------------------------------

    @on(OptionList.OptionSelected, "#plugins-installed")
    def _installed_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if 0 <= event.option_index < len(self.plugin_ids):
            self.open_settings(self.plugin_ids[event.option_index])

    @on(OptionList.OptionSelected, "#plugins-store")
    def _store_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if self.model is None or self._busy():
            return
        if 0 <= event.option_index < len(self.store_ids):
            self.model.install_from_store(self.store_ids[event.option_index])

    @on(OptionList.OptionSelected, "#plugins-indexes")
    def _index_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()

    @on(Button.Pressed, "#plugins-toggle")
    def _toggle_button(self, event: Button.Pressed) -> None:
        event.stop()
        self._toggle_highlighted()

    @on(Button.Pressed, "#plugins-settings")
    def _settings_button(self, event: Button.Pressed) -> None:
        event.stop()
        self.open_settings(self._highlighted("#plugins-installed", self.plugin_ids))

    @on(Button.Pressed, "#plugins-update")
    def _update_button(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_update_plugin()

    @on(Button.Pressed, "#plugins-remove")
    def _remove_button(self, event: Button.Pressed) -> None:
        event.stop()
        self._remove_highlighted()

    @on(Button.Pressed, "#plugins-log")
    def _log_button(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_show_log()

    @on(Button.Pressed, "#plugins-refresh")
    def _refresh_button(self, event: Button.Pressed) -> None:
        event.stop()
        if self.model is not None:
            self.model.clear_message()
            self.model.load()

    @on(Button.Pressed, "#plugins-store-refresh")
    def _store_refresh_button(self, event: Button.Pressed) -> None:
        event.stop()
        if self.model is not None:
            self.model.load_store(True)

    @on(Button.Pressed, "#plugins-install-url")
    @on(Input.Submitted, "#plugins-url")
    @on(Input.Submitted, "#plugins-ref")
    def _install_url(self, event: Button.Pressed | Input.Submitted) -> None:
        event.stop()
        url = self.query_one("#plugins-url", Input).value.strip()
        if not present.is_https(url):
            self.query_one("#plugins-message", Static).update(Text(
                _("Type an https:// Git URL first."), style=_ERROR))
            self.query_one("#plugins-message", Static).display = True
            return
        if self.model is not None and not self._busy():
            self.model.prepare_install(url, self.query_one("#plugins-ref", Input).value.strip())

    @on(Button.Pressed, "#plugins-add-index")
    @on(Input.Submitted, "#plugins-index-url")
    def _add_index(self, event: Button.Pressed | Input.Submitted) -> None:
        event.stop()
        field = self.query_one("#plugins-index-url", Input)
        url = field.value.strip()
        if present.is_https(url) and self.model is not None and not self._busy():
            self.model.set_indexes([*self.index_urls, url])
            field.value = ""

    @on(Button.Pressed, "#plugins-default-index")
    def _default_index(self, event: Button.Pressed) -> None:
        event.stop()
        if self.model is not None and not self._busy():
            self.model.set_indexes([self.model.state()["defaultIndex"]])


# ---- dialogs ------------------------------------------------------------------------


class InstallConfirmScreen(ModalScreen[bool]):
    """What an install or update runs, before pip does anything."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", _("Cancel"), show=False),
    ]

    def __init__(self, pending: Mapping[str, Any]) -> None:
        super().__init__()
        self.pending = dict(pending)

    def compose(self) -> ComposeResult:
        heading, confirm = present.install_texts(self.pending)
        text = Text()
        for row in self.pending.get("rows") or []:
            if text:
                text.append("\n")
            text.append(f"{_plain(row['label'])}: ", style="bold #7dd3fc")
            text.append(_lines(row["value"]))
        with Vertical(id="plugin-install-dialog", classes="dialog"):
            yield Static(heading, classes="dialog-title")
            with VerticalScroll(id="plugin-install-rows"):
                yield Static(text, classes="dialog-copy")
            with Horizontal(classes="dialog-actions"):
                yield Button(_("Cancel"), id="plugin-install-cancel")
                yield Button(confirm, variant="primary", id="plugin-install-confirm")

    def on_mount(self) -> None:
        self.query_one("#plugin-install-cancel", Button).focus()

    @on(Button.Pressed, "#plugin-install-confirm")
    def _confirm(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#plugin-install-cancel")
    def _cancel(self) -> None:
        self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(False)


class RemoveConfirmScreen(ModalScreen[bool]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", _("Cancel"), show=False),
    ]

    def __init__(self, name: str) -> None:
        super().__init__()
        self.plugin_name = name

    def compose(self) -> ComposeResult:
        with Vertical(id="plugin-remove-dialog", classes="dialog"):
            yield Static(_("Remove Plugin?"), classes="dialog-title")
            yield Static(Text(_plain(self.plugin_name), style="bold"))
            yield Static(Text(_("Removes the plugin's program and files. Its own settings "
                                "and keyring entries stay.")), classes="dialog-copy")
            with Horizontal(classes="dialog-actions"):
                yield Button(_("Cancel"), id="plugin-remove-cancel")
                yield Button(_("Remove"), variant="error", id="plugin-remove-confirm")

    def on_mount(self) -> None:
        self.query_one("#plugin-remove-cancel", Button).focus()

    @on(Button.Pressed, "#plugin-remove-confirm")
    def _confirm(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#plugin-remove-cancel")
    def _cancel(self) -> None:
        self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(False)


class PluginLogScreen(ModalScreen[None]):
    """The plugin's standard log file (plugin API 1.4), plain text, newest last."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape,g", "close", _("Close"), show=False),
        Binding("r", "reload", _("Refresh"), show=False),
    ]

    def __init__(self, plugin_id: str, name: str, read_log: LogReader = plugin_logs.tail):
        super().__init__()
        self.plugin_id = plugin_id
        self.plugin_name = name
        self._read_log = read_log

    def compose(self) -> ComposeResult:
        with Vertical(id="plugin-log-dialog", classes="dialog"):
            yield Static(Text(_plain(self.plugin_name)), classes="dialog-title")
            yield Static("", id="plugin-log-path", classes="options-caption")
            with VerticalScroll(id="plugin-log-scroll"):
                yield Static(Text(_("Loading…"), style="dim"), id="plugin-log-text")
            with Horizontal(classes="dialog-actions"):
                yield Button(_("Refresh"), id="plugin-log-reload")
                yield Button(_("Close"), variant="primary", id="plugin-log-close")

    def on_mount(self) -> None:
        self.action_reload()

    def action_reload(self) -> None:
        TextualTaskRunner(self, group="plugin-log").submit(
            lambda: self._read_log(self.plugin_id), self._loaded, self._failed)

    def _loaded(self, value: tuple[Any, str]) -> None:
        path, text = value
        body = self.query_one("#plugin-log-text", Static)
        if path is None:
            self.query_one("#plugin-log-path", Static).update("")
            body.update(Text(plugin_logs.missing_text(), style="dim"))
            return
        self.query_one("#plugin-log-path", Static).update(Text(_plain(path)))
        # The plugin masks secrets; the text still never reaches markup.
        body.update(Text(_lines(text)))
        self.query_one("#plugin-log-scroll", VerticalScroll).scroll_end(animate=False)

    def _failed(self, message: str) -> None:
        self.query_one("#plugin-log-text", Static).update(Text(_plain(message), style=_ERROR))

    @on(Button.Pressed, "#plugin-log-reload")
    def _reload_button(self) -> None:
        self.action_reload()

    @on(Button.Pressed, "#plugin-log-close")
    def _close_button(self) -> None:
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)


# ---- the settings form ------------------------------------------------------------


class _Field:
    """The widgets of one form field."""

    def __init__(self, field: Mapping[str, Any], box: Vertical, error: Static,
                 notes: Static | None) -> None:
        self.field = field
        self.box = box
        self.error = error
        self.notes = notes


class PluginConfigScreen(ModalScreen[None]):
    """A plugin's settings form; typed values stay in :attr:`draft` until Save.

    The fields are built again only when the model's fields, sections or
    buttons change, so a status line or a check never rebuilds them under
    the cursor.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", _("Close"), show=False),
        Binding("ctrl+s", "save", _("Save"), show=False),
    ]

    def __init__(self, model: PluginSettingsModel, config: Mapping[str, Any]) -> None:
        super().__init__()
        self._model = model
        self.plugin_id = str(config["id"])
        self._config: Mapping[str, Any] = config
        self._busy = False
        self.draft: dict[str, object] = {}
        self.edited: set[str] = set()
        self.expanded: dict[str, bool] = {}
        self.check: dict[str, Any] = {"errors": {}, "visible": [], "valid": False}
        self._fields_key: str | None = None
        self._errors_key: str | None = None
        self._generation = 0
        self.fields: dict[str, _Field] = {}
        # (group, members box, help, toggle button)
        self.sections: list[tuple[Mapping[str, Any], Vertical, Static, Button]] = []
        self.custom_buttons: dict[str, Button] = {}
        # Widget id -> field key, and the values the fields started with.
        self._widget_keys: dict[str, str] = {}
        self._initial: dict[str, object] = {}
        self._shown = False

    def compose(self) -> ComposeResult:
        with Vertical(id="plugin-config-dialog", classes="dialog"):
            yield Static(Text(_plain(self._config.get("name") or _("Plugin Settings"))),
                         classes="dialog-title", id="plugin-config-title")
            with VerticalScroll(id="plugin-config-body"):
                yield Static(Text(_("Loading the plugin's settings…"), style="dim"),
                             id="plugin-config-loading")
                yield Static("", id="plugin-config-form-error")
                yield Vertical(id="plugin-config-custom")
                yield Static(Text(_("These buttons use the saved settings: save your "
                                    "changes first."), style="dim"),
                             id="plugin-config-unsaved")
                yield Static("", id="plugin-config-action-status")
                yield Vertical(id="plugin-config-sections")
            yield Static("", id="plugin-config-status")
            yield Static(Text(_("Fill in the fields marked * to save."), style="dim"),
                         id="plugin-config-fill")
            with Horizontal(classes="dialog-actions"):
                yield Button(_("Close"), id="plugin-config-close")
                yield Button(_("Cancel Sign-in"), id="plugin-config-cancel-sign-in")
                yield Button("", id="plugin-config-sign-in")
                yield Button(_("Test Connection"), id="plugin-config-test")
                yield Button(_("Save"), variant="primary", id="plugin-config-save")

    def on_mount(self) -> None:
        self._shown = True
        self.show_config(self._config, bool(self._model.state()["busy"]))

    def on_unmount(self) -> None:
        self._shown = False

    # ---- model state ----------------------------------------------------------------

    def show_config(self, config: Mapping[str, Any], busy: bool) -> None:
        if config.get("id") != self.plugin_id:
            return
        self._config, self._busy = config, busy
        if not self._shown:
            return
        self.query_one("#plugin-config-title", Static).update(
            Text(_plain(config.get("name") or _("Plugin Settings"))))
        key = json.dumps([config.get("fields"), config.get("groups"), config.get("actions")],
                         sort_keys=True, default=str)
        if key != self._fields_key:
            self._fields_key = key
            self._build(config)
        errors_key = json.dumps(config.get("errors") or {}, sort_keys=True)
        if errors_key != self._errors_key:
            # The plugin answered: its old reasons no longer apply to edits.
            self._errors_key = errors_key
            self.edited = set()
        self._recheck()
        self._sync()

    def _recheck(self) -> None:
        if self._config.get("loaded") is True:
            self.check = self._model.check_config(self.plugin_id, self.draft) or {
                "errors": {}, "visible": [], "valid": False}

    def set_value(self, key: str, value: object) -> None:
        """An edit: kept in the draft and checked at once. A widget that
        reports the value it was built with is no edit."""
        if key not in self.draft and value == self._initial.get(key):
            return
        self.draft[key] = value
        self.edited.add(key)
        self._recheck()
        self._sync()

    # ---- building ---------------------------------------------------------------------

    def _build(self, config: Mapping[str, Any]) -> None:
        self._generation += 1
        prefix = f"cfg{self._generation}"
        custom = self.query_one("#plugin-config-custom", Vertical)
        sections = self.query_one("#plugin-config-sections", Vertical)
        custom.remove_children()
        sections.remove_children()
        self.fields, self.sections, self.custom_buttons = {}, [], {}
        self._widget_keys, self._initial = {}, {}
        # [ConfigAction …] buttons first: usually the first thing to do.
        widgets: list[Widget] = []
        for index, action in enumerate((config.get("actions") or {}).get("custom") or []):
            button = Button(Text(_plain(action.get("labelPlain"))),
                            variant="primary" if action.get("primary") else "default",
                            id=f"{prefix}-action-{index}")
            self._widget_keys[button.id or ""] = str(action["id"])
            self.custom_buttons[str(action["id"])] = button
            widgets.append(button)
            if action.get("help"):
                widgets.append(Static(Text(_plain(action["help"]), style="dim"),
                                      classes="options-caption"))
        custom.mount_all(widgets)
        fields = config.get("fields") or []
        boxes: list[Widget] = []
        for group_index, group in enumerate(present.form_groups(config)):
            members = present.group_members(fields, group.get("name") or "")
            if members:
                boxes.append(self._section(f"{prefix}-g{group_index}", group, members))
        sections.mount_all(boxes)

    def _section(self, prefix: str, group: Mapping[str, Any],
                 members: list[Mapping[str, Any]]) -> Vertical:
        title = Static(Text(_plain(group.get("label"))), classes="options-section")
        toggle = Button("", id=f"{prefix}-toggle", classes="plugin-config-toggle")
        heading = Horizontal(title, toggle, classes="plugin-config-heading")
        toggle.display = group.get("collapsed") is True
        self._widget_keys[toggle.id or ""] = str(group.get("name") or "")
        help_text = Static(Text(_plain(group.get("help")), style="dim"),
                           classes="options-caption")
        rows: list[Widget] = []
        for index, field in enumerate(members):
            rows.append(self._field(f"{prefix}-f{index}", field))
        body = Vertical(*rows, classes="plugin-config-rows")
        box = Vertical(heading, help_text, body, classes="plugin-config-section")
        heading.display = bool(group.get("label")) or group.get("collapsed") is True
        self.sections.append((group, body, help_text, toggle))
        return box

    def _field(self, ident: str, field: Mapping[str, Any]) -> Vertical:
        key, kind = str(field["key"]), str(field.get("type") or "string")
        value = self.draft.get(key, field.get("value"))
        title = _plain(present.field_title(field))
        self._widget_keys[ident] = key
        widgets: list[Widget] = []
        control: Widget
        if kind == "bool":
            self._initial[key] = value is True
            control = Checkbox(Text(title), value=value is True, id=ident)
        else:
            widgets.append(Static(Text(title), classes="options-label"))
            if kind == "choice":
                choices = [str(choice) for choice in field.get("choices") or []]
                current = str(value) if str(value) in choices else (choices[0] if choices
                                                                     else None)
                self._initial[key] = current
                control = Select([(Text(_plain(c)), c) for c in choices], value=current
                                 if current is not None else Select.BLANK,
                                 allow_blank=current is None, id=ident)
            elif kind == "secret":
                # A stored secret is never shown: empty means "keep".
                self._initial[key] = ""
                control = Input(value=str(self.draft.get(key, "")), password=True, id=ident)
            elif kind == "int":
                text = "" if value is None or value == "" else str(value)
                self._initial[key] = text
                control = Input(value=text, type="integer", id=ident)
            else:
                text = "" if value is None else str(value)
                self._initial[key] = text
                control = Input(value=text, id=ident)
        widgets.append(control)
        error = Static("", classes="plugin-config-error")
        widgets.append(error)
        notes_lines = present.field_notes(field)
        notes = None
        if notes_lines:
            notes = Static(Text("\n".join(_plain(note) for note in notes_lines), style="dim"),
                           classes="options-caption")
            widgets.append(notes)
        if field.get("helpUrl"):
            link = Button(Text(_plain(present.help_link_label(field))),
                          id=f"{ident}-help", classes="plugin-config-help")
            # Where it leads, as plain text.
            link.tooltip = Text(_plain(field["helpUrl"]))
            self._widget_keys[link.id or ""] = key
            widgets.append(link)
        box = Vertical(*widgets, classes="plugin-config-field")
        self.fields[key] = _Field(field, box, error, notes)
        return box

    # ---- state of the controls --------------------------------------------------------

    def error_of(self, key: str) -> str:
        return present.error_of(key, self.check, self._config.get("errors") or {}, self.edited)

    def _open(self, group: Mapping[str, Any]) -> bool:
        fields = self._config.get("fields") or []
        members = present.group_members(fields, group.get("name") or "")
        has_error = any(self.error_of(str(field["key"])) for field in members)
        return present.group_open(group, self.expanded, has_error)

    def _sync(self) -> None:
        config, check = self._config, self.check
        controls = present.form_controls(config, check, self.draft, self._busy)
        load_error = _plain(config.get("loadError") or "")
        self.query_one("#plugin-config-loading").display = (
            config.get("loaded") is not True and not load_error)
        form_error = _plain((config.get("errors") or {}).get("") or "")
        if load_error:
            form_error = _plain(
                _("Could not load the plugin's settings: {error}").format(error=load_error))
        error_line = self.query_one("#plugin-config-form-error", Static)
        error_line.update(Text(form_error, style=_ERROR))
        error_line.display = bool(form_error)
        for button in self.custom_buttons.values():
            button.disabled = not controls.custom
        self.query_one("#plugin-config-custom").display = bool(self.custom_buttons)
        self.query_one("#plugin-config-unsaved").display = controls.unsaved_note
        status = config.get("status") or {}
        for ident, line in (
            ("#plugin-config-action-status", present.status_line(status, action=True)),
            ("#plugin-config-status", present.status_line(status, action=False)),
        ):
            widget = self.query_one(ident, Static)
            style = {"success": _OK, "error": _ERROR}.get(line.style, "dim")
            widget.update(Text(f"{'… ' if line.spinning else ''}{_plain(line.text)}",
                               style=style))
            widget.display = line.visible
        visible = set(check.get("visible") or [])
        for key, item in self.fields.items():
            reason = self.error_of(key)
            item.box.display = key in visible
            item.error.update(Text(_plain(reason), style=_ERROR))
            item.error.display = bool(reason)
        for group, body, help_text, toggle in self.sections:
            is_open = self._open(group)
            body.display = is_open
            body.disabled = not controls.ready
            help_text.display = bool(group.get("help")) and is_open
            toggle.label = _("Hide") if is_open else _("Show")
        save = self.query_one("#plugin-config-save", Button)
        save.disabled = not controls.save
        test = self.query_one("#plugin-config-test", Button)
        test.display, test.disabled = controls.test_visible, not controls.test
        sign_in = self.query_one("#plugin-config-sign-in", Button)
        sign_in.label = Text(_plain((config.get("actions") or {}).get("loginLabel") or ""))
        sign_in.display, sign_in.disabled = controls.sign_in_visible, not controls.sign_in
        self.query_one("#plugin-config-cancel-sign-in").display = controls.cancel_sign_in
        self.query_one("#plugin-config-fill").display = controls.fill_note

    # ---- events -------------------------------------------------------------------------

    @on(Input.Changed)
    def _input_changed(self, event: Input.Changed) -> None:
        key = self._widget_keys.get(event.input.id or "")
        if key in self.fields:
            self.set_value(key, event.value)

    @on(Checkbox.Changed)
    def _checkbox_changed(self, event: Checkbox.Changed) -> None:
        key = self._widget_keys.get(event.checkbox.id or "")
        if key in self.fields:
            self.set_value(key, bool(event.value))

    @on(Select.Changed)
    def _select_changed(self, event: Select.Changed) -> None:
        key = self._widget_keys.get(event.select.id or "")
        if key in self.fields and isinstance(event.value, str):
            self.set_value(key, event.value)

    @on(Input.Submitted)
    def _submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.action_save()

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        ident = event.button.id or ""
        event.stop()
        if ident == "plugin-config-save":
            self.action_save()
        elif ident == "plugin-config-test":
            self._model.test_config(self.plugin_id, dict(self.draft))
        elif ident == "plugin-config-sign-in":
            self._model.sign_in(self.plugin_id, dict(self.draft))
        elif ident == "plugin-config-cancel-sign-in":
            self._model.cancel_sign_in()
        elif ident == "plugin-config-close":
            self.dismiss(None)
        elif ident.endswith("-help"):
            self._model.open_help(self.plugin_id, self._widget_keys.get(ident, ""))
        elif ident.endswith("-toggle"):
            name = self._widget_keys.get(ident, "")
            group = next((g for g, *_rest in self.sections if (g.get("name") or "") == name),
                         None)
            if group is not None:
                self.expanded[name] = not self._open(group)
                self._sync()
        elif "-action-" in ident:
            self._model.run_action(self.plugin_id, self._widget_keys.get(ident, ""))

    def action_save(self) -> None:
        controls = present.form_controls(self._config, self.check, self.draft, self._busy)
        if controls.save:
            self._model.save_config(self.plugin_id, dict(self.draft))

    def action_close(self) -> None:
        self.dismiss(None)
