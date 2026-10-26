"""Settings > Plugins in the GTK client.

GTK counterpart of the Qt client's ``SettingsPlugins.qml``,
``PluginStoreCard.qml`` and ``PluginConfigForm.qml``, on the shared
:class:`blueferry.plugin_settings_model.PluginSettingsModel`: installed
plugins with the enable kill switch, Show Log, update and remove; the store
from the plugin indexes; installing from any https Git URL; the plugin
lists. Every install and update is confirmed in an ``AdwAlertDialog`` with
the model's summary first. A plugin's settings form (sections, ShowIf,
help links, Test connection, browser sign-in, the plugin's own buttons)
opens as a subpage of the settings dialog and closes the model's form when
it is left.

The model owns every rule about late answers, forms that closed meanwhile
and disabled plugins; this module only draws its state. Everything a
plugin or an index says is shown as plain text: rows use
``use_markup=False``, section titles are plain ``GtkLabel``\\ s (an
``AdwPreferencesGroup`` title is markup), icons are freedesktop names that
passed :func:`blueferry.plugin_api.surfaces.icon_name`.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from gi.repository import Adw, Gtk, Pango

from blueferry.i18n import _
from blueferry.plugin_api.surfaces import icon_name
from blueferry.plugin_settings_model import PluginSettingsModel
from blueferry.ui import plugin_settings_presenter as present
from blueferry.ui.util import glib_timer, launch_uri

ModelFactory = Callable[[Callable[[], None], Callable[[str], None]], PluginSettingsModel]


def default_model(client) -> ModelFactory:
    """The model on its own single worker (git, pip and plugin calls block)."""

    def make(on_change: Callable[[], None], opener: Callable[[str], None]) -> PluginSettingsModel:
        return PluginSettingsModel(
            runner=client.task_runner("plugins"), timer=glib_timer, opener=opener,
            on_change=on_change,
        )

    return make


def _label(text: str = "", *css: str, wrap: bool = True) -> Gtk.Label:
    return Gtk.Label(
        label=text, xalign=0, wrap=wrap, wrap_mode=Pango.WrapMode.WORD_CHAR,
        use_markup=False, css_classes=list(css),
    )


def _button(label: str, icon: str = "", *css: str) -> Gtk.Button:
    """A button with a plain-text label and an optional theme icon."""
    button = Gtk.Button(valign=Gtk.Align.CENTER, css_classes=list(css))
    checked = icon_name(icon)
    if checked:
        button.set_child(Adw.ButtonContent(label=label, icon_name=checked))
    else:
        button.set_label(label)
    button.update_property([Gtk.AccessibleProperty.LABEL], [label])
    return button


def _icon_button(icon: str, tooltip: str) -> Gtk.Button:
    button = Gtk.Button(
        icon_name=icon, tooltip_text=tooltip, valign=Gtk.Align.CENTER, css_classes=["flat"],
    )
    button.update_property([Gtk.AccessibleProperty.LABEL], [tooltip])
    return button


def _set_style(widget: Gtk.Widget, style: str) -> None:
    for name in ("success", "error", "warning", "dim-label"):
        widget.remove_css_class(name)
    if style:
        widget.add_css_class(style)


def _clear(group: Adw.PreferencesGroup, rows: list[Gtk.Widget]) -> None:
    for row in rows:
        group.remove(row)
    rows.clear()


class PluginSettingsPage:
    """Fills the "plugins" page of :class:`blueferry.ui.status.SettingsDialog`."""

    def __init__(
        self,
        dialog: Adw.PreferencesDialog,
        page: Adw.PreferencesPage,
        model: ModelFactory,
        *,
        opener: Callable[[str], None] | None = None,
    ) -> None:
        self._dialog = dialog
        self.page = page
        self._launch = opener or (lambda uri: launch_uri(
            dialog, uri, lambda message: self._toast(message)))
        self.model = model(self._changed, self._open)
        self.form: PluginConfigPage | None = None
        self.install_dialog: Adw.AlertDialog | None = None
        self._install_key = ""
        self._listeners: list[Callable[[dict], None]] = []
        self._expanded: set[str] = set()
        self._plugins_key = ""
        self._store_key = ""
        self._indexes_key = ""
        self._plugin_rows: list[Gtk.Widget] = []
        self._plugin_widgets: dict[str, dict[str, Gtk.Widget]] = {}
        self._store_rows: list[Gtk.Widget] = []
        self._store_buttons: list[tuple[Gtk.Button, dict]] = []
        self._index_rows: list[Gtk.Widget] = []
        self._index_buttons: list[Gtk.Button] = []
        self._syncing = False

        # ---- message: git's and pip's last line, a plugin's errors ------
        self.message_group = Adw.PreferencesGroup(visible=False)
        self.message_row = Adw.ActionRow(use_markup=False, title_lines=0)
        self.message_icon = Gtk.Image()
        self.message_row.add_prefix(self.message_icon)
        close = _icon_button("window-close-symbolic", _("Dismiss"))
        close.connect("clicked", lambda _b: self.model.clear_message())
        self.message_row.add_suffix(close)
        self.message_group.add(self.message_row)
        page.add(self.message_group)

        # ---- installed --------------------------------------------------
        self.installed_group = Adw.PreferencesGroup(title=_("Installed"))
        header = Gtk.Box(spacing=6)
        self.spinner = Gtk.Spinner(valign=Gtk.Align.CENTER)
        header.append(self.spinner)
        self.refresh_button = _icon_button("view-refresh-symbolic", _("Refresh"))
        self.refresh_button.connect("clicked", lambda _b: self.model.load())
        header.append(self.refresh_button)
        self.installed_group.set_header_suffix(header)
        page.add(self.installed_group)

        # ---- store ------------------------------------------------------
        self.store_group = Adw.PreferencesGroup(title=_("Add Plugins"))
        self.store_refresh = _icon_button("view-refresh-symbolic", _("Reload the plugin list"))
        self.store_refresh.connect("clicked", lambda _b: self.model.load_store(True))
        self.store_group.set_header_suffix(self.store_refresh)
        page.add(self.store_group)

        # ---- own URL ----------------------------------------------------
        url_group = Adw.PreferencesGroup(title=_("Own URL"))
        self.url_row = Adw.EntryRow(
            title=_("Git URL (https://github.com/…/blueferry-plugin-…)"), use_markup=False,
            input_purpose=Gtk.InputPurpose.URL,
        )
        self.ref_row = Adw.EntryRow(
            title=_("Tag or commit (empty: the newest version)"), use_markup=False,
        )
        self.install_button = _button(_("Check and Install…"), "list-add-symbolic")
        self.install_button.connect("clicked", lambda _b: self._prepare_from_url())
        self.url_row.connect("changed", lambda _r: self._sync_controls())
        self.url_row.connect("entry-activated", lambda _r: self._prepare_from_url())
        install_row = Adw.ActionRow(
            title=_("Shows what the plugin installs and asks first"), use_markup=False,
        )
        install_row.add_suffix(self.install_button)
        for row in (self.url_row, self.ref_row, install_row):
            url_group.add(row)
        page.add(url_group)

        # ---- plugin lists -----------------------------------------------
        self.index_group = Adw.PreferencesGroup(title=_("Plugin Lists"))
        self.default_button = _button(_("Default"))
        self.default_button.connect("clicked", lambda _b: self.model.set_indexes(
            [self.model.state()["defaultIndex"]]))
        self.index_group.set_header_suffix(self.default_button)
        self.index_entry = Adw.EntryRow(
            title=_("Add a list (https://… plugins-index.json)"), use_markup=False,
            input_purpose=Gtk.InputPurpose.URL,
        )
        self.add_index_button = _button(_("Add List"))
        self.add_index_button.connect("clicked", lambda _b: self._add_index())
        self.index_entry.add_suffix(self.add_index_button)
        self.index_entry.connect("changed", lambda _r: self._sync_controls())
        self.index_entry.connect("entry-activated", lambda _r: self._add_index())
        self.index_group.add(self.index_entry)
        page.add(self.index_group)

        self._render()

    # ---- the dialog's side ------------------------------------------------

    def add_listener(self, listener: Callable[[dict], None]) -> None:
        """Call ``listener(state)`` after every change (the window reloads its
        plugin parts when the set of enabled plugins changes)."""
        self._listeners.append(listener)

    def shown(self) -> None:
        """The category became visible: read the plugins and the store."""
        self.model.load()
        self.model.load_store(False)

    def stop(self) -> None:
        """The dialog closed: end the form (and a sign-in) and an install."""
        if self.form is not None:
            self.form = None
            self.model.close_config()
            self._dialog.pop_subpage()
        if self.model.state()["pending"]:
            self.model.cancel_install()

    def _toast(self, text: str) -> None:
        self._dialog.add_toast(Adw.Toast(title=text, use_markup=False))

    def _open(self, uri: str) -> None:
        # Only what the model hands over: checked https links and log files.
        self._launch(uri)

    # ---- actions ------------------------------------------------------------

    def _prepare_from_url(self) -> None:
        url = self.url_row.get_text().strip()
        if present.is_https(url) and not self._busy:
            self.model.prepare_install(url, self.ref_row.get_text().strip())

    def _add_index(self) -> None:
        url = self.index_entry.get_text().strip()
        if not present.is_https(url) or self._busy:
            return
        self.model.set_indexes([*self.model.state()["indexes"], url])
        self.index_entry.set_text("")

    def open_settings(self, plugin_id: str) -> None:
        """Settings of one plugin: the form as a subpage of the dialog."""
        self.model.load_config(plugin_id)
        config = self.model.state()["config"]
        if config.get("id") != plugin_id:
            return  # disabled meanwhile, or no settings
        self.form = PluginConfigPage(self.model, config, self._form_hidden)
        self.form.render(config, self._busy)
        self._dialog.push_subpage(self.form)

    def _form_hidden(self, form: PluginConfigPage) -> None:
        if self.form is form:
            self.form = None
            self.model.close_config()

    def _confirm_remove(self, plugin_id: str) -> None:
        dialog = Adw.AlertDialog(
            heading=_("Remove Plugin?"),
            body=_("Removes the plugin's program and files. Its own settings and "
                   "keyring entries stay."),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("remove", _("Remove"))
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, response: (
            self.model.remove(plugin_id) if response == "remove" else None))
        dialog.present(self._dialog)

    # ---- rendering ----------------------------------------------------------

    @property
    def _busy(self) -> bool:
        return bool(self.model.state()["busy"])

    def _changed(self) -> None:
        self._render()
        state = self.model.state()
        for listener in self._listeners:
            listener(state)

    def _render(self) -> None:
        state = self.model.state()
        message = state["message"]
        self.message_group.set_visible(bool(message))
        self.message_row.set_title(message)
        ok = state["messageOk"] is not False
        self.message_icon.set_from_icon_name(
            "object-select-symbolic" if ok else "dialog-error-symbolic")
        _set_style(self.message_icon, "success" if ok else "error")
        self.spinner.set_spinning(bool(state["busy"]))
        self.spinner.set_visible(bool(state["busy"]))
        self._render_plugins(state)
        self._render_store(state)
        self._render_indexes(state)
        self._sync_controls()
        self._sync_install_dialog(state["pending"])
        config = state["config"]
        if self.form is not None:
            if config.get("id") == self.form.plugin_id:
                self.form.render(config, bool(state["busy"]))
            else:
                # Saved, disabled or removed: the model closed the form.
                self.form = None
                self._dialog.pop_subpage()

    def _render_plugins(self, state: dict) -> None:
        key = json.dumps([state["plugins"], state["ignored"], state["loaded"]], sort_keys=True)
        if key == self._plugins_key:
            return
        self._plugins_key = key
        _clear(self.installed_group, self._plugin_rows)
        self._plugin_widgets = {}
        if state["loaded"] and not state["plugins"]:
            row = Adw.ActionRow(
                title=_("No plugins installed. Add one from the list below."),
                use_markup=False, title_lines=0,
            )
            self._plugin_rows.append(row)
            self.installed_group.add(row)
        for plugin in state["plugins"]:
            row = self._plugin_row(plugin)
            self._plugin_rows.append(row)
            self.installed_group.add(row)
        for ignored in state["ignored"]:
            row = Adw.ActionRow(
                title=_("Ignored {name}").format(name=ignored["name"]),
                subtitle=ignored["reason"], use_markup=False, subtitle_lines=0,
                css_classes=["dim-label"],
            )
            self._plugin_rows.append(row)
            self.installed_group.add(row)

    def _plugin_row(self, plugin: dict) -> Adw.ExpanderRow:
        plugin_id = plugin["id"]
        row = Adw.ExpanderRow(
            title=present.plugin_title(plugin), subtitle=present.plugin_subtitle(plugin),
            use_markup=False, expanded=plugin_id in self._expanded,
        )
        row.connect("notify::expanded", lambda r, _p: (
            self._expanded.add(plugin_id) if r.get_expanded()
            else self._expanded.discard(plugin_id)))
        switch = Gtk.Switch(
            active=plugin["enabled"] is True, valign=Gtk.Align.CENTER, tooltip_text=_("Enabled"),
        )
        switch.update_property([Gtk.AccessibleProperty.LABEL], [_("Enabled")])
        switch.connect("notify::active", lambda s, _p: self._toggled(plugin_id, s.get_active()))
        row.add_suffix(switch)
        if plugin.get("detail"):
            row.add_row(Adw.ActionRow(
                title=_("Status"), subtitle=plugin["detail"], use_markup=False,
                subtitle_lines=0, css_classes=["property"],
            ))
        if present.is_https(plugin.get("source")):
            source = Adw.ActionRow(
                title=_("Source"), subtitle=plugin["source"], use_markup=False,
                css_classes=["property"],
            )
            link = _icon_button("web-browser-symbolic", _("Open the source"))
            link.connect("clicked", lambda _b, url=plugin["source"]: (
                self._launch(url) if present.is_https(url) else None))
            source.add_suffix(link)
            row.add_row(source)
        if not plugin.get("managed"):
            row.add_row(Adw.ActionRow(
                title=_("Installed outside BlueFerry; install it from its URL to manage "
                        "updates here."),
                use_markup=False, title_lines=0, css_classes=["dim-label"],
            ))
        buttons = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE, column_spacing=6, row_spacing=6,
            margin_top=6, margin_bottom=6, margin_start=12, margin_end=12,
            max_children_per_line=4,
        )
        settings = _button(_("Settings"), "emblem-system-symbolic")
        settings.set_visible(plugin.get("hasConfig") is True)
        settings.connect("clicked", lambda _b: self.open_settings(plugin_id))
        log = _button(_("Show Log"), "text-x-generic-symbolic")
        log.set_tooltip_text(_("Open what the plugin wrote to its log file"))
        log.connect("clicked", lambda _b: self.model.open_log(plugin_id))
        update = _button(_("Check for Update"), "software-update-available-symbolic")
        update.connect("clicked", lambda _b: self.model.prepare_update(plugin_id))
        remove = _button(_("Remove"), "user-trash-symbolic", "destructive-action")
        remove.connect("clicked", lambda _b: self._confirm_remove(plugin_id))
        for button in (settings, log, update, remove):
            buttons.append(button)
        for button in (update, remove):
            button.get_parent().set_visible(plugin.get("managed") is True)
        settings.get_parent().set_visible(plugin.get("hasConfig") is True)
        buttons_row = Gtk.ListBoxRow(activatable=False, selectable=False, child=buttons)
        row.add_row(buttons_row)
        self._plugin_widgets[plugin_id] = {
            "switch": switch, "settings": settings, "log": log, "update": update,
            "remove": remove,
        }
        return row

    def _toggled(self, plugin_id: str, enabled: bool) -> None:
        if self._syncing:
            return
        # Setting a switch back from inside its own notify confuses
        # GtkSwitch's state; the switches follow the model once it answers.
        self._syncing = True
        try:
            self.model.set_enabled(plugin_id, enabled)
        finally:
            self._syncing = False

    def _render_store(self, state: dict) -> None:
        store = state["store"]
        key = json.dumps(store, sort_keys=True)
        if key == self._store_key:
            return
        self._store_key = key
        _clear(self.store_group, self._store_rows)
        self._store_buttons = []
        for problem in store["problems"]:
            row = Adw.ActionRow(
                title=f"{problem['problem']} ({problem['index']})", use_markup=False,
                title_lines=0,
            )
            row.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic", css_classes=["warning"]))
            self._store_rows.append(row)
            self.store_group.add(row)
        for card in store["items"]:
            row = Adw.ActionRow(
                title=card["name"], subtitle=present.store_subtitle(card), use_markup=False,
                subtitle_lines=0,
            )
            if card.get("emoji"):
                row.add_prefix(Gtk.Label(label=card["emoji"], use_markup=False,
                                         css_classes=["title-2"]))
            else:
                row.add_prefix(Gtk.Image(
                    icon_name=icon_name(card.get("icon")) or "application-x-addon",
                    pixel_size=32,
                ))
            action = present.store_action(card, False)
            button = _button(action.label, action.icon)
            button.connect("clicked", lambda _b, ident=card["id"]: self.model.install_from_store(
                ident))
            row.add_suffix(button)
            if card.get("state") == "soon":
                row.set_opacity(0.6)
            self._store_buttons.append((button, card))
            self._store_rows.append(row)
            self.store_group.add(row)
        if store["loaded"] and not store["items"]:
            row = Adw.ActionRow(
                title=_("The plugin list is empty or could not be loaded."), use_markup=False,
                title_lines=0,
            )
            self._store_rows.append(row)
            self.store_group.add(row)

    def _render_indexes(self, state: dict) -> None:
        key = json.dumps(state["indexes"])
        if key == self._indexes_key:
            return
        self._indexes_key = key
        _clear(self.index_group, self._index_rows)
        self._index_buttons = []
        for url in state["indexes"]:
            row = Adw.ActionRow(title=url, use_markup=False)
            remove = _icon_button("list-remove-symbolic", _("Remove list"))
            remove.connect("clicked", lambda _b, gone=url: self.model.set_indexes(
                [other for other in self.model.state()["indexes"] if other != gone]))
            row.add_suffix(remove)
            self._index_buttons.append(remove)
            self._index_rows.append(row)
            # Above the entry that adds another list.
            self.index_group.remove(self.index_entry)
            self.index_group.add(row)
            self.index_group.add(self.index_entry)

    def _sync_controls(self) -> None:
        """Sensitivity and switch positions; no row is rebuilt."""
        state = self.model.state()
        busy = bool(state["busy"])
        toggling, self._syncing = self._syncing, True
        try:
            rows = {plugin["id"]: plugin for plugin in state["plugins"]}
            for plugin_id, widgets in self._plugin_widgets.items():
                plugin = rows.get(plugin_id, {})
                enabled = plugin.get("enabled") is True
                if not toggling:
                    widgets["switch"].set_active(enabled)
                widgets["switch"].set_sensitive(not busy)
                widgets["settings"].set_sensitive(not busy and enabled)
                for name in ("log", "update", "remove"):
                    widgets[name].set_sensitive(not busy)
        finally:
            self._syncing = toggling
        for button, card in self._store_buttons:
            button.set_sensitive(present.store_action(card, busy).sensitive)
        for button in (self.refresh_button, self.store_refresh, self.default_button,
                       *self._index_buttons):
            button.set_sensitive(not busy)
        self.install_button.set_sensitive(
            not busy and present.is_https(self.url_row.get_text()))
        self.add_index_button.set_sensitive(
            not busy and present.is_https(self.index_entry.get_text()))

    # ---- install confirmation -------------------------------------------------

    def _sync_install_dialog(self, pending: Mapping[str, Any]) -> None:
        key = json.dumps(pending, sort_keys=True) if pending.get("id") else ""
        if key == self._install_key:
            return
        self._install_key = key
        if self.install_dialog is not None:
            old, self.install_dialog = self.install_dialog, None
            old.force_close()
        if key:
            self.install_dialog = self._install_prompt(pending)
            self.install_dialog.present(self._dialog)

    def _install_prompt(self, pending: Mapping[str, Any]) -> Adw.AlertDialog:
        heading, confirm = present.install_texts(pending)
        dialog = Adw.AlertDialog(
            heading=heading,
            body=_("A plugin runs as your user with access to your files and network. "
                   "Install only plugins from sources you trust."),
        )
        grid = Gtk.Grid(column_spacing=12, row_spacing=6)
        for line, row in enumerate(pending.get("rows") or []):
            grid.attach(_label(f"{row['label']}:", "dim-label", wrap=False), 0, line, 1, 1)
            value = _label(str(row["value"]))
            value.set_max_width_chars(40)
            value.set_hexpand(True)
            grid.attach(value, 1, line, 1, 1)
        dialog.set_extra_child(grid)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("install", confirm)
        dialog.set_response_appearance("install", Adw.ResponseAppearance.SUGGESTED)
        # Enter must not install on its own.
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def responded(current: Adw.AlertDialog, response: str) -> None:
            if self.install_dialog is not current:
                return
            self.install_dialog = None
            if response == "install":
                self.model.confirm_install()
            else:
                self.model.cancel_install()

        dialog.connect("response", responded)
        return dialog


class PluginConfigPage(Adw.NavigationPage):
    """One plugin's settings form (``state.config``) as a dialog subpage.

    Fields are built again only when the plugin's schema or the loaded
    values change, so a status line or a check never rebuilds them under
    the cursor. Typed values stay in :attr:`draft` until Save.
    """

    def __init__(
        self, model: PluginSettingsModel, config: dict,
        on_hidden: Callable[[PluginConfigPage], None],
    ) -> None:
        super().__init__(title=config.get("name") or _("Plugin Settings"), tag="plugin-config")
        self._model = model
        self.plugin_id: str = config["id"]
        self.draft: dict[str, object] = {}
        self.edited: set[str] = set()
        self.expanded: dict[str, bool] = {}
        self.check: dict[str, Any] = {"errors": {}, "visible": [], "valid": False}
        self._config: dict = config
        self._busy = False
        self._fields_key: str | None = None
        self._errors_key: str | None = None
        self._building = False
        # key -> (input row, notes row, error label)
        self.inputs: dict[str, tuple[Adw.PreferencesRow, Gtk.ListBoxRow, Gtk.Label]] = {}
        self.sections: list[tuple[dict, Gtk.Box, Gtk.ListBox, Gtk.Label, Gtk.Button]] = []
        self.custom_buttons: dict[str, Gtk.Button] = {}
        self._noted: set[str] = set()

        self.body = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=18,
            margin_top=12, margin_bottom=24, margin_start=12, margin_end=12,
        )
        self.loading = _label(_("Loading the plugin's settings…"), "dim-label")
        self.form_error = _label("", "error")
        self.actions_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.custom_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.unsaved_note = _label(
            _("These buttons use the saved settings: save your changes first."),
            "dim-label", "caption",
        )
        self.action_status, self.action_spinner, self.action_text = self._status_box()
        for widget in (self.custom_box, self.unsaved_note, self.action_status):
            self.actions_box.append(widget)
        self.sections_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.status, self.status_spinner, self.status_text = self._status_box()

        buttons = Gtk.Box(spacing=6)
        self.save_button = _button(_("Save"), "document-save-symbolic", "suggested-action")
        self.save_button.connect("clicked", lambda _b: self._model.save_config(
            self.plugin_id, dict(self.draft)))
        self.test_button = _button(_("Test Connection"), "network-transmit-receive-symbolic")
        self.test_button.connect("clicked", lambda _b: self._model.test_config(
            self.plugin_id, dict(self.draft)))
        self.sign_in_button = _button(
            (config.get("actions") or {}).get("loginLabel") or "", "web-browser-symbolic")
        self.sign_in_button.connect("clicked", lambda _b: self._model.sign_in(
            self.plugin_id, dict(self.draft)))
        self.cancel_sign_in_button = _button(_("Cancel Sign-in"), "process-stop-symbolic")
        self.cancel_sign_in_button.connect("clicked", lambda _b: self._model.cancel_sign_in())
        for button in (self.save_button, self.test_button, self.sign_in_button,
                       self.cancel_sign_in_button):
            buttons.append(button)
        self.fill_note = _label(_("Fill in the fields marked * to save."), "dim-label", "caption")

        for widget in (self.loading, self.form_error, self.actions_box, self.sections_box,
                       self.status, buttons, self.fill_note):
            self.body.append(widget)
        clamp = Adw.Clamp(maximum_size=640, child=self.body)
        scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True, child=clamp,
        )
        toolbar = Adw.ToolbarView(content=scroller)
        toolbar.add_top_bar(Adw.HeaderBar())
        self.set_child(toolbar)
        self.connect("hidden", lambda _page: on_hidden(self))

    @staticmethod
    def _status_box() -> tuple[Gtk.Box, Gtk.Spinner, Gtk.Label]:
        box = Gtk.Box(spacing=6, visible=False)
        spinner = Gtk.Spinner(valign=Gtk.Align.CENTER)
        text = _label()
        text.set_hexpand(True)
        box.append(spinner)
        box.append(text)
        return box, spinner, text

    # ---- model state ----------------------------------------------------------

    def render(self, config: dict, busy: bool) -> None:
        if config.get("id") != self.plugin_id:
            return
        self._config, self._busy = config, busy
        self.set_title(config.get("name") or _("Plugin Settings"))
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
        """An edit: kept in the draft and checked at once."""
        if self._building:
            return
        self.draft[key] = value
        self.edited.add(key)
        self._recheck()
        self._sync()

    # ---- building ---------------------------------------------------------------

    def _build(self, config: dict) -> None:
        self._building = True
        try:
            for box in (self.custom_box, self.sections_box):
                child = box.get_first_child()
                while child is not None:
                    box.remove(child)
                    child = box.get_first_child()
            self.inputs, self.sections, self.custom_buttons = {}, [], {}
            self._noted = set()
            # [ConfigAction …] buttons first: usually the first thing to do.
            for action in (config.get("actions") or {}).get("custom") or []:
                button = _button(
                    str(action.get("labelPlain") or ""), str(action.get("icon") or ""),
                    *(["suggested-action"] if action.get("primary") else []),
                )
                button.set_halign(Gtk.Align.START)
                button.connect("clicked", lambda _b, ident=action["id"]: self._model.run_action(
                    self.plugin_id, ident))
                self.custom_box.append(button)
                self.custom_buttons[action["id"]] = button
                if action.get("help"):
                    self.custom_box.append(_label(action["help"], "dim-label", "caption"))
            fields = config.get("fields") or []
            for group in present.form_groups(config):
                members = present.group_members(fields, group.get("name") or "")
                if members:
                    self.sections_box.append(self._section(group, members))
        finally:
            self._building = False

    def _section(self, group: dict, members: list) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        heading = Gtk.Box(spacing=6, visible=bool(group.get("label")))
        title = _label(str(group.get("label") or ""), "heading")
        title.set_hexpand(True)
        heading.append(title)
        toggle = Gtk.Button(css_classes=["flat"], visible=group.get("collapsed") is True)
        toggle.connect("clicked", lambda _b: self._toggle(group))
        heading.append(toggle)
        box.append(heading)
        help_label = _label(str(group.get("help") or ""), "dim-label")
        box.append(help_label)
        rows = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        for field in members:
            row = self._input(field)
            notes, error = self._notes(field)
            self.inputs[field["key"]] = (row, notes, error)
            rows.append(row)
            rows.append(notes)
        box.append(rows)
        self.sections.append((group, box, rows, help_label, toggle))
        return box

    def _toggle(self, group: dict) -> None:
        name = str(group.get("name") or "")
        self.expanded[name] = not self._open(group)
        self._sync()

    def _input(self, field: dict) -> Adw.PreferencesRow:
        key, kind = field["key"], field["type"]
        value = self.draft.get(key, field.get("value"))
        title = present.field_title(field)
        row: Adw.PreferencesRow
        if kind == "bool":
            row = Adw.SwitchRow(title=title, use_markup=False, active=value is True)
            row.connect("notify::active", lambda r, _p: self.set_value(key, r.get_active()))
        elif kind == "int":
            low, high = int(field.get("minimum", 0)), int(field.get("maximum", 0))
            try:
                number = int(value)  # type: ignore[call-overload]
            except (TypeError, ValueError):
                number = 0
            adjustment = Gtk.Adjustment(
                lower=low, upper=high, step_increment=1, page_increment=10,
                value=min(max(number, low), high),
            )
            row = Adw.SpinRow(
                title=title, use_markup=False, adjustment=adjustment, digits=0, numeric=True,
            )
            row.connect("notify::value", lambda r, _p: self.set_value(key, int(r.get_value())))
        elif kind == "choice":
            choices = [str(choice) for choice in field.get("choices") or []]
            row = Adw.ComboRow(
                title=title, use_markup=False, model=Gtk.StringList.new(choices),
            )
            if value in choices:
                row.set_selected(choices.index(str(value)))
            row.connect("notify::selected", lambda r, _p: self._chosen(key, choices, r))
        elif kind == "secret":
            # A stored secret is never shown: empty means "keep".
            row = Adw.PasswordEntryRow(title=title, use_markup=False)
            row.set_text(str(self.draft.get(key, "")))
            row.connect("changed", lambda r: self.set_value(key, r.get_text()))
        else:
            row = Adw.EntryRow(title=title, use_markup=False, text=str(value or ""))
            if kind == "url":
                row.set_input_purpose(Gtk.InputPurpose.URL)
            row.connect("changed", lambda r: self.set_value(key, r.get_text()))
        if field.get("help"):
            row.update_property([Gtk.AccessibleProperty.DESCRIPTION], [str(field["help"])])
        return row

    def _chosen(self, key: str, choices: list[str], row: Adw.ComboRow) -> None:
        selected = row.get_selected()
        if 0 <= selected < len(choices):
            self.set_value(key, choices[selected])

    def _notes(self, field: dict) -> tuple[Gtk.ListBoxRow, Gtk.Label]:
        """The row under a field: its error, the grey notes and the help link."""
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=2,
            margin_top=6, margin_bottom=6, margin_start=12, margin_end=12,
        )
        error = _label("", "error")
        box.append(error)
        for note in present.field_notes(field):
            box.append(_label(note, "dim-label", "caption"))
        if field.get("helpUrl"):
            link = Gtk.Button(halign=Gtk.Align.START, css_classes=["flat"])
            label = present.help_link_label(field)
            link.set_child(Adw.ButtonContent(label=label, icon_name="help-browser-symbolic"))
            # The tooltip shows where it leads; plain text.
            link.set_tooltip_text(str(field["helpUrl"]))
            link.connect("clicked", lambda _b, key=field["key"]: self._model.open_help(
                self.plugin_id, key))
            box.append(link)
        row = Gtk.ListBoxRow(activatable=False, selectable=False, child=box)
        if present.field_notes(field) or field.get("helpUrl"):
            self._noted.add(field["key"])
        return row, error

    # ---- state of the controls ------------------------------------------------------

    def error_of(self, key: str) -> str:
        return present.error_of(key, self.check, self._config.get("errors") or {}, self.edited)

    def _open(self, group: dict) -> bool:
        fields = self._config.get("fields") or []
        members = present.group_members(fields, group.get("name") or "")
        has_error = any(self.error_of(field["key"]) for field in members)
        return present.group_open(group, self.expanded, has_error)

    def _sync(self) -> None:
        config, check = self._config, self.check
        controls = present.form_controls(config, check, self.draft, self._busy)
        loaded = config.get("loaded") is True
        self.loading.set_visible(not loaded)
        form_error = str((config.get("errors") or {}).get("") or "")
        self.form_error.set_label(form_error)
        self.form_error.set_visible(bool(form_error))

        self.actions_box.set_visible(bool(self.custom_buttons))
        for button in self.custom_buttons.values():
            button.set_sensitive(controls.custom)
        self.unsaved_note.set_visible(controls.unsaved_note)
        status = config.get("status") or {}
        for line, (box, spinner, text) in (
            (present.status_line(status, action=True),
             (self.action_status, self.action_spinner, self.action_text)),
            (present.status_line(status, action=False),
             (self.status, self.status_spinner, self.status_text)),
        ):
            box.set_visible(line.visible)
            text.set_label(line.text)
            _set_style(text, line.style)
            spinner.set_visible(line.spinning)
            spinner.set_spinning(line.spinning)

        visible = set(check.get("visible") or [])
        for key, (row, notes, error) in self.inputs.items():
            shown = key in visible
            reason = self.error_of(key)
            row.set_visible(shown)
            if reason:
                row.add_css_class("error")
            else:
                row.remove_css_class("error")
            error.set_label(reason)
            error.set_visible(bool(reason))
            notes.set_visible(shown and (bool(reason) or key in self._noted))
        for group, _box, rows, help_label, toggle in self.sections:
            is_open = self._open(group)
            rows.set_visible(is_open)
            rows.set_sensitive(controls.ready)
            help_label.set_visible(bool(group.get("help")) and is_open)
            toggle.set_label(_("Hide") if is_open else _("Show"))

        self.save_button.set_sensitive(controls.save)
        self.test_button.set_visible(controls.test_visible)
        self.test_button.set_sensitive(controls.test)
        self.sign_in_button.set_visible(controls.sign_in_visible)
        self.sign_in_button.set_sensitive(controls.sign_in)
        self.cancel_sign_in_button.set_visible(controls.cancel_sign_in)
        self.fill_note.set_visible(controls.fill_note)
