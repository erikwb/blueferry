"""Settings > Plugins in the Textual client (fakes only)."""
from __future__ import annotations

from pathlib import Path

from textual.app import App
from textual.widgets import Button, Checkbox, Input, OptionList, Static

from blueferry import plugin_logs
from blueferry.plugin_index import PluginIndex
from blueferry.plugin_manager import PluginManager
from blueferry.plugin_settings_model import PluginSettingsModel
from blueferry.tui_plugin_settings import (
    InstallConfirmScreen,
    PluginConfigScreen,
    PluginLogScreen,
    PluginsPane,
    RemoveConfirmScreen,
)
from tests.client_fakes import ManualRunner
from tests.plugin_settings_fakes import INDEX, DemoClient
from tests.test_plugin_manager import URL, FakeRunner, FakeStopper
from tests.test_tui import _run_headless


class _Host(App[None]):
    CSS_PATH = "../src/blueferry/tui.tcss"

    def __init__(self, pane: PluginsPane) -> None:
        super().__init__()
        self.pane = pane

    def compose(self):
        yield self.pane


def _factory(work: ManualRunner, **kwargs):
    def make(on_change, opener) -> PluginSettingsModel:
        return PluginSettingsModel(runner=work, timer=work.timer, opener=opener,
                                   on_change=on_change, **kwargs)

    return make


async def _drain(pilot, work: ManualRunner) -> None:
    for _round in range(5):
        work.run_tasks()
        await pilot.pause()


def _text(screen, selector: str) -> str:
    return screen.query_one(selector, Static).render().plain


def test_store_install_is_confirmed_and_plugins_toggle_remove_and_log(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    manager = PluginManager(data_home=tmp_path / "data", settings_path=tmp_path / "p.json",
                            runner=FakeRunner(), python="/usr/bin/python3",
                            stopper=FakeStopper())
    index = PluginIndex(fetch=lambda _url: INDEX, cache=tmp_path / "cache")
    work = ManualRunner()
    logs: list[str] = []

    def read_log(plugin_id: str):
        logs.append(plugin_id)
        if len(logs) == 1:
            return None, ""
        return Path("/state/demo.log"), "[b]started[/b]\x1b[2J\napi_key=***"

    pane = PluginsPane(_factory(work, manager=lambda: manager, index=lambda: index,
                                client=DemoClient), read_log=read_log)

    async def scenario() -> None:
        app = _Host(pane)
        async with app.run_test(size=(110, 60)) as pilot:
            await _drain(pilot, work)
            store = app.query_one("#plugins-store", OptionList)
            lines = [store.get_option_at_index(i).prompt.plain
                     for i in range(store.option_count)]
            assert lines[0].startswith("Demo photos  [Install]")
            assert "Coming soon" in lines[1]
            installed = app.query_one("#plugins-installed", OptionList)
            assert "No plugins installed" in installed.get_option_at_index(0).prompt.plain

            store.focus()
            store.highlighted = 0
            await pilot.press("enter")
            await _drain(pilot, work)
            assert isinstance(app.screen, InstallConfirmScreen)
            copy = _text(app.screen, ".dialog-copy")
            assert f"Source: {URL}" in copy and "Capabilities: photos" in copy
            await pilot.click("#plugin-install-confirm")
            await _drain(pilot, work)
            assert not isinstance(app.screen, InstallConfirmScreen)
            assert "Installed Demo photos" in _text(app, "#plugins-message")
            assert pane.plugin_ids == ["io.example.demo"]
            line = installed.get_option_at_index(0).prompt.plain
            assert line.startswith("● Demo photos") and "Needs setup" in line

            installed.focus()
            installed.highlighted = 0
            await pilot.press("g")
            await pilot.pause()
            assert isinstance(app.screen, PluginLogScreen)
            await _until(pilot, lambda: plugin_logs.missing_text() in _text(
                app.screen, "#plugin-log-text"))
            await pilot.press("r")
            await _until(pilot, lambda: "started" in _text(app.screen, "#plugin-log-text"))
            body = _text(app.screen, "#plugin-log-text")
            assert "[b]started[/b]" in body and "\x1b" not in body
            await pilot.press("escape")
            await pilot.pause()

            installed.focus()
            await pilot.press("space")
            await _drain(pilot, work)
            assert manager.disabled() == {"io.example.demo"}
            assert installed.get_option_at_index(0).prompt.plain.startswith("○ Demo photos")

            await pilot.press("delete")
            await pilot.pause()
            assert isinstance(app.screen, RemoveConfirmScreen)
            await pilot.click("#plugin-remove-confirm")
            await _drain(pilot, work)
            assert pane.plugin_ids == []
            assert "Removed the plugin" in _text(app, "#plugins-message")

            app.query_one("#plugins-url", Input).value = "http://insecure.example/x"
            await pilot.click("#plugins-install-url")
            await pilot.pause()
            assert "https://" in _text(app, "#plugins-message")
            assert not work.pending

    _run_headless(scenario())


async def _until(pilot, condition) -> None:
    for _attempt in range(60):
        if condition():
            return
        await pilot.pause(0.05)
    assert condition()


def test_config_form_is_guided_and_never_shows_a_stored_secret() -> None:
    from tests.test_gtk_plugin_settings import _guided

    _plugin, manager, client = _guided()
    work = ManualRunner()
    opened: list[str] = []
    pane = PluginsPane(_factory(work, manager=lambda: manager, index=lambda: None,
                                client=client, poll_ms=5), opener=opened.append)

    async def scenario() -> None:
        app = _Host(pane)
        async with app.run_test(size=(110, 60)) as pilot:
            await _drain(pilot, work)
            assert pane.plugin_ids == ["io.example.cloud"]
            pane.open_settings("io.example.cloud")
            await pilot.pause()
            form = app.screen
            assert isinstance(form, PluginConfigScreen)
            await _drain(pilot, work)
            assert set(form.fields) == {"url", "app_password", "timeout", "mode"}
            save = form.query_one("#plugin-config-save", Button)
            assert save.disabled and form.query_one("#plugin-config-fill").display
            # ShowIf: "mode" only for that server.
            assert not form.fields["mode"].box.display
            secret = form.fields["app_password"].box.query_one(Input)
            assert secret.password and secret.value == ""
            notes = form.fields["url"].notes
            assert notes is not None and "e.g. https://cloud.example.com" in str(
                notes.render())
            # The Advanced section starts folded.
            advanced = next(body for group, body, _h, _t in form.sections
                            if group.get("name") == "advanced")
            assert not advanced.display

            url = form.fields["url"].box.query_one(Input)
            url.value = "https://cloud.example"
            await pilot.pause()
            assert form.fields["mode"].box.display
            secret.value = "app-secret"
            await pilot.pause()
            assert not save.disabled
            await pilot.click("#plugin-config-test")
            await _drain(pilot, work)
            assert ("test", {"url": "https://cloud.example",
                             "app_password": "app-secret"}) in client.log
            assert "Connected as anna." in _text(form, "#plugin-config-status")

            await pilot.click("#plugin-config-sign-in")
            await _drain(pilot, work)
            assert opened == ["https://cloud.example/login/v2/f"]
            # The sign-in never sees a secret.
            login = next(entry[1] for entry in client.log if entry[0] == "login")
            assert "app_password" not in login

            form.action_save()
            await _drain(pilot, work)
            saved = next(entry[1] for entry in client.log if entry[0] == "save")
            assert saved["app_password"] == "app-secret"
            assert not isinstance(app.screen, PluginConfigScreen)  # saved: closed
            assert pane.form is None

    _run_headless(scenario())


def test_closing_the_form_closes_the_models_form() -> None:
    from tests.test_gtk_plugin_settings import _guided

    _plugin, manager, client = _guided()
    work = ManualRunner()
    pane = PluginsPane(_factory(work, manager=lambda: manager, index=lambda: None,
                                client=client))

    async def scenario() -> None:
        app = _Host(pane)
        async with app.run_test(size=(110, 60)) as pilot:
            await _drain(pilot, work)
            pane.open_settings("io.example.cloud")
            await _drain(pilot, work)
            form = app.screen
            assert isinstance(form, PluginConfigScreen)
            checkbox = form.query(Checkbox)
            assert not checkbox  # no bool fields in this manifest
            await pilot.press("escape")
            await pilot.pause()
            assert pane.model is not None and pane.model.state()["config"] == {}

    _run_headless(scenario())
