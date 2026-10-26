"""GTK Settings > Plugins on a private Broadway display: installed plugins,
the store, the confirmed install, the plugin lists, the settings form as a
subpage (sections, ShowIf, Test connection, sign-in, the plugin's own
buttons) and that plugin text never becomes markup.

The shared model runs on a :class:`tests.client_fakes.ManualRunner`, so each
scenario decides when the "worker" answers.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from blueferry.ui import plugin_settings_presenter as present
from tests.gtk_broadway import run_scenario


@pytest.mark.parametrize(
    "scenario", ["store-install", "installed", "form", "actions", "lists", "close"],
)
def test_gtk_plugin_settings(tmp_path, scenario):
    run_scenario(tmp_path, str(Path(__file__).resolve()), scenario)


# ---- pure rules (no display) --------------------------------------------------


def test_https_and_store_rules() -> None:
    assert present.is_https("https://git.example.org/x")
    assert not present.is_https("http://git.example.org/x")
    assert not present.is_https("https://a b")
    card = {"state": "update", "stateText": "Update available", "installable": True,
            "description": "Photos.", "badges": ["Photos", "Files"],
            "installedRef": "v0.1.0", "ref": "v0.2.0"}
    assert present.store_subtitle(card) == "Photos.\nPhotos · Files\nv0.1.0 → v0.2.0"
    assert present.store_action(card, False) == present.StoreAction(
        "Update available", "software-update-available-symbolic", True)
    assert present.store_action(card, True).sensitive is False
    assert present.store_action({"state": "installed", "stateText": "Installed ✓"}, False) \
        == present.StoreAction("Installed ✓", "", False)
    assert present.install_texts({"kind": "update"}) == ("Update Plugin?", "Update")
    assert present.install_texts({"kind": "install"}) == ("Install Plugin?", "Install")
    row = {"name": "Demo", "version": "1.0", "stateText": "Ready",
           "capabilities": ["Photos"], "ref": "v1"}
    assert present.plugin_title(row) == "Demo  1.0"
    assert present.plugin_subtitle(row) == "Ready · Photos · version v1"


def test_form_rules_follow_the_qt_form() -> None:
    config = {"loaded": True, "actions": {"test": True, "loginLabel": "Sign in",
                                          "custom": [{"id": "a"}]},
              "status": {}}
    valid = {"errors": {}, "valid": True}
    controls = present.form_controls(config, valid, {}, False)
    assert not controls.save and controls.test and controls.custom
    assert controls.sign_in_visible and not controls.cancel_sign_in and not controls.fill_note
    edited = present.form_controls(config, valid, {"url": "x"}, False)
    assert edited.save and not edited.custom and edited.unsaved_note
    signing = present.form_controls(
        {**config, "status": {"kind": "login", "pending": True}}, valid, {}, False)
    assert signing.cancel_sign_in and not signing.sign_in_visible and not signing.test
    invalid = present.form_controls(config, {"errors": {}, "valid": False}, {}, False)
    assert invalid.fill_note and not invalid.test
    assert not present.form_controls(config, valid, {"u": 1}, True).save  # busy

    assert present.group_open({"name": "advanced", "collapsed": True}, {}, False) is False
    assert present.group_open({"name": "advanced", "collapsed": True}, {}, True) is True
    assert present.group_open({"name": "advanced", "collapsed": True},
                              {"advanced": False}, True) is False
    assert present.field_notes({"type": "secret", "stored": True, "placeholder": "x"}) == [
        "Stored — leave empty to keep"]
    assert present.field_notes({"type": "url", "placeholder": "https://a", "help": "H",
                                "example": "E"}) == ["e.g. https://a", "H", "Example: E"]
    assert present.help_link_label({"helpUrlLabelPlain": "Which <b>port</b>?"}) \
        == "Which <b>port</b>?"
    assert present.help_link_label({}) == "More information"
    check = {"errors": {"a": "bad"}}
    assert present.error_of("a", check, {"a": "old"}, set()) == "bad"
    assert present.error_of("b", check, {"b": "plugin"}, set()) == "plugin"
    assert present.error_of("b", check, {"b": "plugin"}, {"b"}) == ""
    line = present.status_line({"kind": "test", "text": "Connected", "ok": True}, action=False)
    assert line == present.StatusLine(True, "Connected", "success", False)
    assert not present.status_line({"kind": "action", "text": "x"}, action=False).visible
    pending = present.status_line({"kind": "action", "text": "Working…", "pending": True},
                                  action=True)
    assert pending.spinning and pending.style == ""


# ---- widget scenarios (child process) --------------------------------------------


class FakeClient:
    """The daemon side the settings dialog needs; nothing answers."""

    def connect(self, *_args):
        return 0

    def record_status(self, _status):
        pass

    def get_status_async(self, on_ok, _on_err=None):
        from blueferry.models import BackendStatus

        on_ok(BackendStatus(daemon=True))

    def features_async(self, *_args):
        pass

    def notification_open_map_async(self, *_args):
        pass

    def unlock_storage_async(self, *_args):
        pass


def _dialog(**model_options):
    """The settings dialog with a plugin model on a ManualRunner."""
    from types import SimpleNamespace
    from unittest.mock import patch

    from blueferry.plugin_settings_model import PluginSettingsModel
    from blueferry.setup_client import ConfigurationState
    from blueferry.ui import status as status_module
    from tests.client_fakes import ManualRunner

    Adw, Gtk = status_module.Adw, status_module.Gtk
    Adw.init()
    Gtk.Settings.get_default().set_property("gtk-enable-animations", False)
    work = ManualRunner()
    opened: list[str] = []
    models = []

    def factory(on_change, opener):
        model = PluginSettingsModel(runner=work, timer=work.timer, opener=opener,
                                    on_change=on_change, **model_options)
        models.append(model)
        return model

    configuration = ConfigurationState.from_dict({"configured": True, "saved": True})
    with (
        patch.object(status_module, "SetupClient",
                     lambda: SimpleNamespace(configuration=lambda: configuration)),
        patch.object(status_module.SettingsDialog, "_load_setup_state", lambda self: None),
    ):
        dialog = status_module.SettingsDialog(FakeClient(), plugin_settings=factory)
    dialog.plugins._launch = opened.append
    window = Adw.ApplicationWindow(default_width=760, default_height=760)
    window.present()
    return dialog, window, work, opened


def _settle():
    import time

    from gi.repository import GLib

    context = GLib.MainContext.default()
    deadline = time.monotonic() + 0.3
    while time.monotonic() < deadline:
        if not context.iteration(False):
            time.sleep(0.01)


def _descendants(widget):
    child = widget.get_first_child()
    while child is not None:
        yield child
        yield from _descendants(child)
        child = child.get_next_sibling()


def _markup_free(widget):
    """No label under ``widget`` reads markup, except the PreferencesGroup
    titles (ours, translated)."""
    from gi.repository import Adw, Gtk

    for label in _descendants(widget):
        if isinstance(label, Gtk.Label) and label.get_use_markup() and label.get_label():
            group = label.get_ancestor(Adw.PreferencesGroup)
            assert group is not None and label.get_label() in (
                group.get_title(), group.get_description()), label.get_label()
    for row in _descendants(widget):
        if isinstance(row, Adw.PreferencesRow) and not isinstance(row, Adw.EntryRow):
            assert row.get_use_markup() is False, row.get_title()


def _manager(tmp, *, stopper_state=None):
    from blueferry.plugin_index import PluginIndex
    from blueferry.plugin_manager import PluginManager
    from tests.plugin_settings_fakes import INDEX
    from tests.test_plugin_manager import FakeRunner, FakeStopper

    runner = FakeRunner()
    manager = PluginManager(
        data_home=tmp / "data", settings_path=tmp / "p.json", runner=runner,
        python="/usr/bin/python3", stopper=FakeStopper(),
    )
    index = PluginIndex(fetch=lambda _url: INDEX, cache=tmp / "cache")
    manager.runner = runner  # type: ignore[attr-defined]
    return manager, index


def _tmp():
    import os
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    os.environ["XDG_DATA_HOME"] = str(tmp / "data")
    return tmp


def _exercise_store_install():
    from tests.plugin_settings_fakes import DemoClient

    tmp = _tmp()
    manager, index = _manager(tmp)
    dialog, window, work, _opened = _dialog(
        manager=lambda: manager, index=lambda: index, client=DemoClient)
    plugins = dialog.plugins
    assert "plugins" in dialog.pages
    assert dialog.pages["plugins"].get_title() == "Plugins"
    assert not work.pending, "nothing is read before the category opens"
    dialog.open(window, "plugins")
    _settle()
    assert plugins.spinner.get_spinning()
    assert not plugins.refresh_button.get_sensitive()
    work.drain()
    assert not plugins.spinner.get_spinning()
    titles = [row.get_title() for row in plugins._plugin_rows]
    assert titles == ["No plugins installed. Add one from the list below."]
    store = {row.get_title(): row for row in plugins._store_rows}
    assert set(store) == {"Demo photos", "Calendar"}
    assert store["Calendar"].get_opacity() < 1
    buttons = {card["id"]: button for button, card in plugins._store_buttons}
    assert buttons["io.example.demo"].get_sensitive()
    assert not buttons["io.example.cal"].get_sensitive()  # coming soon

    # Install: the summary first, pip only after the confirmation.
    buttons["io.example.demo"].emit("clicked")
    work.drain()
    _settle()
    prompt = plugins.install_dialog
    assert prompt is not None and prompt.get_heading() == "Install Plugin?"
    values = [w.get_label() for w in _descendants(prompt.get_extra_child())]
    assert "Source:" in values and "https://git.example.org/me/blueferry-plugin-demo" in values
    assert prompt.get_default_response() == "cancel"
    assert not manager.records()
    prompt.emit("response", "install")
    work.drain()
    _settle()
    assert plugins.install_dialog is None
    assert "Installed Demo photos" in plugins.message_row.get_title()
    assert plugins.message_group.get_visible()
    [row] = plugins._plugin_rows
    assert row.get_title() == "Demo photos  0.1.0"
    assert "Needs setup" in row.get_subtitle()
    plugins.message_row.get_first_child()  # the row exists
    plugins.model.clear_message()
    assert not plugins.message_group.get_visible()
    _markup_free(dialog.pages["plugins"])

    # Cancel discards the checkout.
    from tests.test_plugin_manager import NEW

    manager.runner.tags["v0.2.0"] = NEW
    plugins.model.prepare_update("io.example.demo")
    work.drain()
    _settle()
    assert plugins.install_dialog is not None
    assert plugins.install_dialog.get_heading() == "Update Plugin?"
    plugins.install_dialog.emit("response", "cancel")
    work.drain()
    assert plugins.model.state()["pending"] == {} and plugins.install_dialog is None
    dialog.close()
    _settle()
    window.destroy()


def _exercise_installed():
    from blueferry.plugin_api.manifest import parse_manifest
    from tests.plugin_settings_fakes import DemoClient
    from tests.test_plugin_settings_model import _OneManager

    plugin = parse_manifest(
        "[BlueFerry Plugin]\nId=io.example.cloud\nName=<b>Cloud</b>\nVersion=1.0\n"
        "ApiVersion=1.3\nMinBlueFerry=0.1\nCapabilities=card;\n"
        "Homepage=https://example.org/plugin\nExec=example-plugin serve\n"
        "[Config url]\nLabel=Server URL\nType=url\n"
    )

    class Manager(_OneManager):
        enabled = True
        refuse = False
        removed: list[str] = []  # noqa: RUF012

        def entries(self):
            from blueferry.plugin_manager import PluginEntry

            return [PluginEntry(self.plugin, None, Manager.enabled)], [
                ("broken.plugin", "<i>no Exec</i>")]

        def set_enabled(self, plugin_id, enabled):
            from blueferry.plugin_manager import InstallError

            if Manager.refuse:
                raise InstallError("settings file is read-only")
            Manager.enabled = enabled

        def remove(self, plugin_id):
            from types import SimpleNamespace

            Manager.removed.append(plugin_id)
            return SimpleNamespace(stop=None)

    manager = Manager(plugin)
    dialog, window, work, opened = _dialog(
        manager=lambda: manager, index=lambda: None, client=DemoClient)
    plugins = dialog.plugins
    dialog.open(window, "plugins")
    _settle()
    work.run_tasks()  # the store fails (no index): a message, not a crash
    row, ignored = plugins._plugin_rows
    assert row.get_title() == "<b>Cloud</b>  1.0"  # literal, never markup
    assert ignored.get_title() == "Ignored broken.plugin"
    assert ignored.get_subtitle() == "<i>no Exec</i>"
    widgets = plugins._plugin_widgets["io.example.cloud"]
    # Installed outside BlueFerry: no update or remove.
    assert not widgets["update"].get_parent().get_visible()
    assert widgets["settings"].get_parent().get_visible()
    assert widgets["switch"].get_active()
    widgets["log"].emit("clicked")
    work.run_tasks()
    assert "has not written a log yet" in plugins.message_row.get_title()
    _markup_free(dialog.pages["plugins"])

    # The kill switch: the plugin's settings close and it is never called.
    widgets["settings"].emit("clicked")
    work.run_tasks()
    _settle()
    assert plugins.form is not None
    widgets["switch"].set_active(False)
    _settle()
    assert plugins.form is None and plugins.model.state()["config"] == {}
    # The switch keeps the new position while the change runs; it never
    # flips back to the old value before the list is read again.
    work.run_next()
    assert not widgets["switch"].get_active()
    assert plugins._plugin_widgets["io.example.cloud"] is widgets
    work.run_tasks()
    widgets = plugins._plugin_widgets["io.example.cloud"]
    assert not widgets["switch"].get_active() and not widgets["settings"].get_sensitive()
    assert "Disabled" in plugins._plugin_rows[0].get_subtitle()
    # A change that fails shows the real state again.
    Manager.refuse = True
    widgets["switch"].set_active(True)
    assert widgets["switch"].get_active()
    work.run_tasks()
    assert "read-only" in plugins.message_row.get_title()
    assert not widgets["switch"].get_active()
    assert opened == []
    dialog.close()
    _settle()
    window.destroy()


def _guided():
    from blueferry.plugin_api.manifest import parse_manifest
    from tests.plugin_settings_fakes import GUIDED, GuidedClient
    from tests.test_plugin_settings_model import _OneManager

    plugin = parse_manifest(
        "[BlueFerry Plugin]\nId=io.example.cloud\nName=Cloud\nVersion=1.0\n"
        "ApiVersion=1.3\nMinBlueFerry=0.1\nCapabilities=card;\n"
        "Homepage=https://example.org/plugin\nExec=example-plugin serve\n"
        "ConfigTest=true\nConfigLogin=nextcloud\n" + GUIDED +
        "\n[Config mode]\nLabel=Mode <b>x</b>\nType=choice\nChoices=fast;safe\n"
        "Default=fast\nShowIf=url=https://cloud.example\n"
    )
    GuidedClient.log, GuidedClient.polls = [], 0
    return plugin, _OneManager(plugin), GuidedClient


def _rows_of(form):
    return {key: row for key, (row, _notes, _error) in form.inputs.items()}


def _exercise_form():
    from blueferry.ui.status import Adw

    _plugin, manager, client = _guided()
    dialog, window, work, opened = _dialog(
        manager=lambda: manager, index=lambda: None, client=client, poll_ms=5)
    plugins = dialog.plugins
    dialog.open(window, "plugins")
    _settle()
    work.run_tasks()
    plugins.open_settings("io.example.cloud")
    _settle()
    form = plugins.form
    assert form is not None and form.get_title() == "Cloud"
    assert form.loading.get_visible() and not form.save_button.get_sensitive()
    work.run_tasks()
    _settle()
    assert not form.loading.get_visible()
    rows = _rows_of(form)
    assert isinstance(rows["url"], Adw.EntryRow) and rows["url"].get_title() == "Server URL *"
    assert isinstance(rows["app_password"], Adw.PasswordEntryRow)
    assert isinstance(rows["timeout"], Adw.SpinRow) and rows["timeout"].get_value() == 15
    assert isinstance(rows["mode"], Adw.ComboRow)
    assert rows["mode"].get_title() == "Mode <b>x</b>"
    assert not rows["mode"].get_visible(), "ShowIf: hidden until the URL matches"
    # Account first, Advanced folded until opened.
    sections = {group.get("name"): (rows_box, toggle)
                for group, _box, rows_box, _help, toggle in form.sections}
    assert sections["account"][0].get_visible()
    assert not sections["advanced"][0].get_visible()
    assert sections["advanced"][1].get_label() == "Show"
    sections["advanced"][1].emit("clicked")
    assert sections["advanced"][0].get_visible()
    # Notes: the placeholder as an example, the help link.
    notes = [w.get_label() for w in _descendants(form.inputs["url"][1])
             if hasattr(w, "get_label") and w.get_label()]
    assert "e.g. https://cloud.example.com" in notes and "More information" in notes
    assert form.sign_in_button.get_visible() and form.test_button.get_visible()
    assert not form.test_button.get_sensitive() and form.fill_note.get_visible()

    # Typing checks at once; Save waits for the required fields.
    rows["url"].set_text("https://cloud.example")
    assert rows["mode"].get_visible()
    assert not form.save_button.get_sensitive()
    rows["app_password"].set_text("secret")
    assert form.save_button.get_sensitive() and form.test_button.get_sensitive()
    rows["url"].set_text("not a url")
    assert rows["url"].has_css_class("error")
    assert form.inputs["url"][2].get_visible()
    rows["url"].set_text("https://down.example")
    form.test_button.emit("clicked")
    assert form.status_text.get_label() == "Testing the connection…"
    assert form.status_spinner.get_spinning()
    work.run_tasks()
    assert form.status_text.get_label() == "The server did not answer."
    assert form.status_text.has_css_class("error")
    assert form.inputs["url"][2].get_label() == "unreachable"
    rows["url"].set_text("https://cloud.example")
    assert form.inputs["url"][2].get_label() == "", "an edit replaces the plugin's reason"

    # The browser sign-in opens the model's checked URL and can be cancelled.
    form.sign_in_button.emit("clicked")
    work.run_tasks()
    assert opened == ["https://cloud.example/login/v2/f"]
    assert form.cancel_sign_in_button.get_visible() and not form.sign_in_button.get_visible()
    form.cancel_sign_in_button.emit("clicked")
    work.run_tasks()
    assert form.status_text.get_label() == "The sign-in was cancelled."
    assert ("cancel", "flow-1") in client.log
    _markup_free(form)

    # Save closes the form (the model), which leaves the subpage.
    form.save_button.emit("clicked")
    work.run_tasks()
    _settle()
    assert plugins.form is None
    # Only what the user typed: the plugin keeps the rest.
    assert [entry for entry in client.log if entry[0] == "save"] == [
        ("save", {"url": "https://cloud.example", "app_password": "secret"})]
    assert "Saved the settings of Cloud." in plugins.message_row.get_title()

    # Leaving the subpage closes the model's form and stops a sign-in.
    plugins.open_settings("io.example.cloud")
    work.run_tasks()
    _settle()
    form = plugins.form
    form.sign_in_button.emit("clicked")
    work.run_tasks()
    timer = plugins.model._login_timer
    assert timer.active
    dialog.pop_subpage()
    _settle()
    assert plugins.form is None and plugins.model.state()["config"] == {}
    assert not timer.active
    dialog.close()
    _settle()
    window.destroy()


def _exercise_actions():
    from blueferry.plugin_api.manifest import parse_manifest
    from tests.test_plugin_settings_model import ACTIONS, _ActionClient, _OneManager

    plugin = parse_manifest(
        "[BlueFerry Plugin]\nId=io.example.setup\nName=Setup\nVersion=1.0\n"
        "ApiVersion=1.6\nMinBlueFerry=0.1\nCapabilities=card;\n"
        "Homepage=https://example.org/plugin\nExec=example-plugin serve\n" + ACTIONS
    )
    _ActionClient.ran, _ActionClient.port = [], 47801
    dialog, window, work, opened = _dialog(
        manager=lambda: _OneManager(plugin), index=lambda: None, client=_ActionClient)
    plugins = dialog.plugins
    dialog.open(window, "plugins")
    _settle()
    work.run_tasks()
    plugins.open_settings("io.example.setup")
    work.run_tasks()
    _settle()
    form = plugins.form
    buttons = form.custom_buttons
    assert list(buttons) == ["setup_iphone", "approve", "broken"]
    labels = [w.get_label() for w in _descendants(buttons["setup_iphone"])
              if hasattr(w, "get_label") and w.get_label()]
    assert "Set up <b>iPhone</b>" in labels, labels  # literal, not escaped, not markup
    assert buttons["setup_iphone"].has_css_class("suggested-action")
    assert all(button.get_sensitive() for button in buttons.values())
    notes = [w.get_label() for w in _descendants(form.inputs["port"][1])
             if hasattr(w, "get_label") and w.get_label()]
    assert "Which port?" in notes
    # Unsaved edits: the buttons wait, with the note why.
    _rows_of(form)["port"].set_value(47900)
    assert not any(button.get_sensitive() for button in buttons.values())
    assert form.unsaved_note.get_visible()
    form.draft.clear()  # as if saved
    form.render(plugins.model.state()["config"], False)
    buttons["setup_iphone"].emit("clicked")
    assert form.action_text.get_label() == "Working…" and form.action_spinner.get_spinning()
    assert not form.status.get_visible(), "an action's answer shows under its buttons"
    work.run_tasks()
    _settle()
    assert opened == ["http://127.0.0.1:4711/abc/"]
    assert form.action_text.get_label() == "Done." and form.action_text.has_css_class("success")
    buttons = plugins.form.custom_buttons
    buttons["broken"].emit("clicked")
    work.run_tasks()
    assert "did not answer" in plugins.form.action_text.get_label()
    assert plugins.form.action_text.has_css_class("error")
    _markup_free(plugins.form)
    dialog.close()
    _settle()
    window.destroy()


def _exercise_lists():
    from tests.plugin_settings_fakes import DemoClient

    tmp = _tmp()
    manager, index = _manager(tmp)
    dialog, window, work, _opened = _dialog(
        manager=lambda: manager, index=lambda: index, client=DemoClient)
    plugins = dialog.plugins
    dialog.open(window, "plugins")
    _settle()
    work.drain()
    assert [row.get_title() for row in plugins._index_rows] == [
        plugins.model.state()["defaultIndex"]]
    assert not plugins.add_index_button.get_sensitive()
    plugins.index_entry.set_text("http://insecure.example/index.json")
    assert not plugins.add_index_button.get_sensitive()
    plugins.index_entry.set_text("https://lists.example/plugins-index.json")
    assert plugins.add_index_button.get_sensitive()
    plugins.add_index_button.emit("clicked")
    work.drain()
    assert [row.get_title() for row in plugins._index_rows][-1] == \
        "https://lists.example/plugins-index.json"
    assert plugins.index_entry.get_text() == ""
    # The entry stays below the lists.
    assert plugins.index_entry.get_next_sibling() is None
    plugins._index_buttons[0].emit("clicked")
    work.drain()
    assert [row.get_title() for row in plugins._index_rows] == [
        "https://lists.example/plugins-index.json"]
    plugins.default_button.emit("clicked")
    work.drain()
    assert [row.get_title() for row in plugins._index_rows] == [
        plugins.model.state()["defaultIndex"]]

    # Own URL: only https starts the confirmed install.
    assert not plugins.install_button.get_sensitive()
    plugins.url_row.set_text("https://git.example.org/me/blueferry-plugin-demo")
    plugins.ref_row.set_text("v0.1.0")
    assert plugins.install_button.get_sensitive()
    plugins.install_button.emit("clicked")
    work.drain()
    _settle()
    assert plugins.install_dialog is not None
    rows = {row["label"]: row["value"] for row in plugins.model.state()["pending"]["rows"]}
    assert rows["Version"].startswith("v0.1.0")
    dialog.close()
    _settle()
    # Closing the dialog cancels the install that waited for an answer.
    assert plugins.model.state()["pending"] == {}
    work.drain()
    window.destroy()


def _exercise_close():
    # The real GLib timer of a sign-in is removed when the dialog closes.
    from blueferry.plugin_settings_model import PluginSettingsModel
    from blueferry.ui.util import glib_timer

    _plugin, manager, client = _guided()
    dialog, window, work, _opened = _dialog(
        manager=lambda: manager, index=lambda: None, client=client, poll_ms=60_000)
    plugins = dialog.plugins
    model: PluginSettingsModel = plugins.model
    model._login_timer = glib_timer(model._poll_login)
    dialog.open(window, "plugins")
    _settle()
    work.run_tasks()
    plugins.open_settings("io.example.cloud")
    work.run_tasks()
    _settle()
    plugins.form.sign_in_button.emit("clicked")
    work.run_tasks()
    assert model._login_timer.active
    dialog.close()
    _settle()
    assert not model._login_timer.active and plugins.form is None
    work.run_tasks()
    assert ("cancel", "flow-1") in client.log
    window.destroy()


if __name__ == "__main__":
    {
        "store-install": _exercise_store_install,
        "installed": _exercise_installed,
        "form": _exercise_form,
        "actions": _exercise_actions,
        "lists": _exercise_lists,
        "close": _exercise_close,
    }[sys.argv[1]]()
