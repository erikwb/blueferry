"""`blueferry plugins`: discovery listing and forwarding; never execs."""
from __future__ import annotations

import pytest
from typer.testing import CliRunner

from blueferry import cli_plugins
from blueferry.cli import app
from blueferry.plugin_api.manifest import Discovery
from blueferry.plugin_api.testing import manifest


@pytest.fixture
def hooks(monkeypatch):
    executed: list[list[str]] = []
    state = {"discovery": Discovery()}
    monkeypatch.setitem(cli_plugins._hooks, "discover", lambda: state["discovery"])
    monkeypatch.setitem(cli_plugins._hooks, "exec", lambda argv: executed.append(list(argv)))
    return state, executed


def test_alias_forwards_every_argument_to_the_plugin_cli(hooks) -> None:
    state, executed = hooks
    state["discovery"] = Discovery((manifest(extra="Cli=immich-cli --x\nAlias=immich\n"),))
    result = CliRunner().invoke(
        app, ["plugins", "immich", "setup", "--url", "https://photos.example.org"],
    )
    assert result.exit_code == 0, result.output
    assert executed == [["immich-cli", "--x", "setup", "--url", "https://photos.example.org"]]


def test_a_disabled_plugin_is_neither_run_nor_configured(hooks, managed, monkeypatch) -> None:
    state, executed = hooks
    manager, _runner, _answers = managed
    plugin = manifest(extra="Cli=immich-cli\nAlias=immich\n[Config url]\nLabel=URL\nType=url\n")
    state["discovery"] = Discovery((plugin,))
    manager.set_enabled(plugin.id, False)
    monkeypatch.setitem(cli_plugins._hooks, "disabled", manager.disabled)
    called: list = []
    monkeypatch.setitem(cli_plugins._hooks, "client", lambda p: called.append(p))
    result = CliRunner().invoke(app, ["plugins", "immich", "setup"])
    assert result.exit_code == 2 and "is disabled" in result.output and executed == []
    result = CliRunner().invoke(app, ["plugins", "config", plugin.id])
    assert result.exit_code == 2 and "enable it first" in result.output and called == []
    result = CliRunner().invoke(app, ["plugins", "disable", plugin.id])
    assert "does not stop it" in result.output  # not installed by BlueFerry


def test_unknown_alias(hooks) -> None:
    _state, executed = hooks
    result = CliRunner().invoke(app, ["plugins", "nope"])
    assert result.exit_code == 2 and "No BlueFerry plugin" in result.output
    assert executed == []


# ---- management commands with fake git/venv (see test_plugin_manager) -----------


@pytest.fixture
def managed(tmp_path, monkeypatch):
    from blueferry.plugin_manager import PluginManager
    from tests.test_plugin_manager import FakeRunner, FakeStopper

    runner = FakeRunner()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    manager = PluginManager(
        data_home=tmp_path / "data", settings_path=tmp_path / "plugins.json",
        runner=runner, python="/usr/bin/python3", stopper=FakeStopper(),
    )
    answers: list[bool] = []
    monkeypatch.setitem(cli_plugins._hooks, "manager", lambda: manager)
    monkeypatch.setitem(cli_plugins._hooks, "confirm", lambda _text: answers.pop(0))
    return manager, runner, answers


URL = "https://git.example.org/me/blueferry-plugin-demo"


def test_install_asks_first_and_shows_source_ref_and_capabilities(managed) -> None:
    manager, runner, answers = managed
    answers.append(False)
    result = CliRunner().invoke(app, ["plugins", "install", URL])
    assert result.exit_code == 1 and "Cancelled" in result.output
    assert "Source:" in result.output and "v0.1.0" in result.output
    assert "Capabilities: photos" in result.output and "blueferry-demo serve" in result.output
    assert "Plugin and dependency code runs with your user permissions" in result.output
    assert "including during installation" in result.output
    assert result.output.index("Capabilities do not restrict access") < result.output.index("Cancelled")
    assert not any("pip" in " ".join(call) for call in runner.calls)
    assert manager.records() == {}
    # "No" throws the checkout away: nothing of the plugin stays on disk.
    assert not list((manager.root / "src").glob(".staging-*"))
    assert not (manager.root / "src" / "io.example.demo").exists()
    result = CliRunner().invoke(app, ["plugins", "install", URL, "--yes"])
    assert result.exit_code == 0, result.output
    assert "Installed Demo photos at v0.1.0" in result.output
    result = CliRunner().invoke(app, ["plugins", "list"])
    assert "io.example.demo 0.1.0" in result.output and f"from {URL} at v0.1.0" in result.output


def test_update_disable_and_remove(managed) -> None:
    manager, runner, answers = managed
    CliRunner().invoke(app, ["plugins", "install", URL, "-y"])
    result = CliRunner().invoke(app, ["plugins", "update", "io.example.demo"])
    assert "up to date" in result.output
    runner.tags["v0.2.0"] = "b" * 40
    answers.append(True)
    result = CliRunner().invoke(app, ["plugins", "update", "io.example.demo"])
    assert result.exit_code == 0, result.output
    assert "v0.1.0 (aaaaaaaaaaaa) -> v0.2.0 (bbbbbbbbbbbb)" in result.output
    assert "bbbbbbb Add videos" in result.output and "Updated Demo photos" in result.output
    assert "restarts on next use" not in result.output  # nothing was running
    manager.stopper.state = "stopped"
    result = CliRunner().invoke(app, ["plugins", "disable", "io.example.demo"])
    assert "io.example.demo disabled.\nStopped the running plugin.\n" == result.output
    assert "[disabled]" in CliRunner().invoke(app, ["plugins", "list"]).output
    answers.append(True)
    result = CliRunner().invoke(app, ["plugins", "remove", "io.example.demo"])
    assert result.exit_code == 0 and "Removed" in result.output
    assert result.output.endswith("Stopped the running plugin.\n")
    assert manager.records() == {}
    result = CliRunner().invoke(app, ["plugins", "install", "http://insecure.example/x", "-y"])
    assert result.exit_code == 1 and "https" in result.output


def test_alias_and_update_without_id(managed) -> None:
    manager, runner, answers = managed
    CliRunner().invoke(app, ["plugins", "install", URL, "-y"])
    result = CliRunner().invoke(app, ["plugins", "update", "demo"])
    assert result.exit_code == 0 and "up to date" in result.output
    runner.tags["v0.2.0"] = "b" * 40
    manager.stopper.state = "stopped"
    result = CliRunner().invoke(app, ["plugins", "update", "--yes"])
    assert result.exit_code == 0, result.output
    assert "Updated Demo photos at v0.2.0.\nStopped the running plugin; it restarts on next use." \
        in result.output
    CliRunner().invoke(app, ["plugins", "disable", "demo"])
    assert manager.disabled() == {"io.example.demo"}
    answers.append(True)
    result = CliRunner().invoke(app, ["plugins", "remove", "demo"])
    assert result.exit_code == 0 and manager.records() == {}


def test_available_search_and_index_commands(managed, monkeypatch, tmp_path) -> None:
    import json

    from blueferry.plugin_index import DEFAULT_INDEX_URL, PluginIndex

    data = json.dumps({"version": 1, "plugins": [
        {"id": "io.example.demo", "name": "Demo photos", "description": "Photos.",
         "repo": URL, "ref": "v0.1.0", "capabilities": ["photos"], "emoji": "📷"},
        {"id": "io.example.cal", "name": "Calendar", "repo": URL + "-cal", "ref": None,
         "capabilities": ["calendar"]},
    ]}).encode()
    monkeypatch.setitem(cli_plugins._hooks, "index",
                        lambda: PluginIndex(fetch=lambda _url: data, cache=tmp_path / "c"))
    result = CliRunner().invoke(app, ["plugins", "available"])
    assert result.exit_code == 0, result.output
    assert "Demo photos  [available v0.1.0]" in result.output
    assert f"blueferry plugins install {URL} --ref v0.1.0" in result.output
    assert "Calendar  [coming soon]" in result.output
    assert "Calendar" not in CliRunner().invoke(app, ["plugins", "search", "photo"]).output
    result = CliRunner().invoke(app, ["plugins", "index", "add", "https://example.org/i.json"])
    assert result.output.split() == [DEFAULT_INDEX_URL, "https://example.org/i.json"]
    result = CliRunner().invoke(app, ["plugins", "index", "add", "http://example.org/i"])
    assert result.exit_code == 2
    result = CliRunner().invoke(app, ["plugins", "index", "remove", DEFAULT_INDEX_URL])
    assert result.output.split() == ["https://example.org/i.json"]


def test_config_reads_and_writes_through_the_plugin(hooks, managed, monkeypatch) -> None:
    from blueferry.plugin_api.client import ConfigResult

    state, _executed = hooks
    plugin = manifest(extra=(
        "[Config url]\nLabel=Server URL\nType=url\nRequired=true\n"
        "[Config api_key]\nLabel=API key\nType=secret\n"
        "[Config videos]\nLabel=Videos\nType=bool\n"
    ))
    state["discovery"] = Discovery((plugin,))
    sent: list[dict] = []

    class Client:
        def __init__(self, _manifest) -> None:
            pass

        def set_config(self, values):
            sent.append(dict(values))
            return ConfigResult(True, {})

        def get_config(self):
            return {"url": "https://a.example", "api_key": "********", "videos": True}

    monkeypatch.setitem(cli_plugins._hooks, "client", Client)
    monkeypatch.setitem(cli_plugins._hooks, "secret", lambda _prompt: " k3y ")
    result = CliRunner().invoke(app, [
        "plugins", "config", "io.example.photos", "--set", "videos=on", "--secret", "api_key",
    ])
    assert result.exit_code == 0, result.output
    assert sent == [{"videos": True, "api_key": "k3y"}]
    assert "api_key = (stored)" in result.output and "k3y" not in result.output
    result = CliRunner().invoke(app, ["plugins", "config", "io.example.photos",
                                      "--set", "api_key=visible"])
    assert result.exit_code == 2


def test_config_test_and_browser_sign_in(hooks, monkeypatch) -> None:
    from blueferry.plugin_api.config_flow import ConfigTestResult, LoginStep

    state, _executed = hooks
    plugin = manifest(api_version="1.3", extra=(
        "ConfigTest=true\nConfigLogin=nextcloud\n"
        "[Config url]\nLabel=Server URL\nType=url\nRequired=true\n"
        "[Config token]\nLabel=App password\nType=secret\n"
    ))
    state["discovery"] = Discovery((plugin,))
    calls: list[tuple] = []

    class Client:
        def __init__(self, _manifest) -> None:
            self.polls = 0

        def test_config(self, values):
            calls.append(("test", dict(values)))
            ok = values.get("url") == "https://good.example"
            return ConfigTestResult(ok, "Connected as anna" if ok else "No answer",
                                    {} if ok else {"url": "unreachable"})

        def config_login(self, values):
            calls.append(("login", dict(values)))
            return LoginStep("open", login_id="f1", open_uri="https://good.example/login/v2/f")

        def config_login_status(self, login_id):
            self.polls += 1
            return LoginStep("done" if self.polls > 1 else "pending", "Connected as anna")

        def config_login_cancel(self, login_id):
            calls.append(("cancel", login_id))

        def set_config(self, values):
            calls.append(("set", dict(values)))

        def get_config(self):
            return {"url": "https://good.example", "token": "********"}

    opened: list[str] = []
    monkeypatch.setitem(cli_plugins._hooks, "client", Client)
    monkeypatch.setitem(cli_plugins._hooks, "open_uri", opened.append)
    monkeypatch.setitem(cli_plugins._hooks, "sleep", lambda _seconds: None)
    result = CliRunner().invoke(app, [
        "plugins", "config", "io.example.photos", "--set", "url=https://good.example", "--test",
    ])
    assert result.exit_code == 0 and "OK: Connected as anna" in result.output
    result = CliRunner().invoke(app, [
        "plugins", "config", "io.example.photos", "--set", "url=https://bad.example", "--test",
    ])
    assert result.exit_code == 1 and "url: unreachable" in result.output
    assert not any(call[0] == "set" for call in calls)
    result = CliRunner().invoke(app, [
        "plugins", "config", "io.example.photos", "--set", "url=https://good.example", "--login",
    ])
    assert result.exit_code == 0, result.output
    assert opened == ["https://good.example/login/v2/f"]
    assert "Connected as anna" in result.output and "token = (stored)" in result.output
    assert ("login", {"url": "https://good.example"}) in calls
    assert not any(call[0] == "set" for call in calls)


def test_config_action_runs_a_settings_button(hooks, monkeypatch) -> None:
    from blueferry.plugin_api.surfaces import ActionResult

    state, _executed = hooks
    plugin = manifest(api_version="1.6", extra=(
        "[Config port]\nLabel=Port\nType=int\n"
        "[ConfigAction setup_iphone]\nLabel=Set up iPhone\nHelp=Shows a QR code.\n"
        "[ConfigAction approve]\nLabel=Approve this network\n"
    ))
    state["discovery"] = Discovery((plugin,))
    ran: list[str] = []

    class Client:
        def __init__(self, _manifest) -> None:
            pass

        def run_config_action(self, action_id):
            ran.append(action_id)
            if action_id == "approve":
                return ActionResult(False, "No network to approve.")
            return ActionResult(True, None, "http://127.0.0.1:4711/abc/")

        def get_config(self):
            return {"port": 47801}

    opened: list[str] = []
    monkeypatch.setitem(cli_plugins._hooks, "client", Client)
    monkeypatch.setitem(cli_plugins._hooks, "open_uri", opened.append)
    result = CliRunner().invoke(app, ["plugins", "config", "io.example.photos"])
    assert result.exit_code == 0, result.output
    assert "--action setup_iphone  # Set up iPhone: Shows a QR code." in result.output
    result = CliRunner().invoke(app, ["plugins", "config", "io.example.photos",
                                      "--action", "setup_iphone"])
    assert result.exit_code == 0, result.output
    assert "Done." in result.output and opened == ["http://127.0.0.1:4711/abc/"]
    result = CliRunner().invoke(app, ["plugins", "config", "io.example.photos",
                                      "--action", "approve"])
    assert result.exit_code == 1 and "No network to approve." in result.output
    result = CliRunner().invoke(app, ["plugins", "config", "io.example.photos",
                                      "--action", "nope"])
    assert result.exit_code == 2 and "it offers: setup_iphone, approve" in result.output
    # The action uses the saved settings: never together with changes.
    result = CliRunner().invoke(app, ["plugins", "config", "io.example.photos",
                                      "--set", "port=1", "--action", "setup_iphone"])
    assert result.exit_code == 2
    assert ran == ["setup_iphone", "approve"]
