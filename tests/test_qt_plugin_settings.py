"""Qt plugin management, feature switches and the doctor: fakes only."""
from __future__ import annotations

import json
from typing import ClassVar

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication
from PySide6.QtGui import QGuiApplication

from blueferry.plugin_api.client import ConfigResult, PluginStatus
from blueferry.plugin_api.config import SECRET_MASK
from blueferry.plugin_index import PluginIndex
from blueferry.plugin_manager import PluginManager
from blueferry.qt.plugin_settings import PluginSettings
from tests.qt_settle import main_thread_gc, settle, wait
from tests.test_plugin_manager import URL, FakeRunner, FakeStopper

# Keep the cyclic GC off the pool threads (see tests/qt_settle.py).
pytestmark = pytest.mark.usefixtures(main_thread_gc.__name__)


@pytest.fixture(scope="module")
def app():
    return QCoreApplication.instance() or QGuiApplication([])


def _wait(predicate) -> None:
    wait(predicate)


class _Client:
    saved: ClassVar[list[dict]] = []

    def __init__(self, manifest) -> None:
        self.manifest = manifest

    def status(self):
        return PluginStatus("unconfigured", "set a server")

    def get_config(self):
        return {"url": "https://a.example", "api_key": SECRET_MASK, "camera_model": ""}

    def set_config(self, values):
        _Client.saved.append(dict(values))
        if values.get("url") == "https://bad.example":
            return ConfigResult(False, {"url": "not reachable"})
        return ConfigResult(True, {})


INDEX = json.dumps({"version": 1, "plugins": [
    {"id": "io.example.demo", "name": "Demo photos", "description": "Photos.",
     "repo": URL, "ref": "v0.1.0", "capabilities": ["photos"], "icon": "folder-pictures"},
    {"id": "io.example.cal", "name": "Calendar", "repo": URL + "-cal", "ref": None,
     "capabilities": ["calendar"]},
]}).encode()


@pytest.fixture
def plugins(app, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    runner = FakeRunner()
    manager = PluginManager(data_home=tmp_path / "data", settings_path=tmp_path / "p.json",
                            runner=runner, python="/usr/bin/python3",
                            stopper=FakeStopper())
    index = PluginIndex(fetch=lambda _url: INDEX, cache=tmp_path / "cache")
    _Client.saved = []
    settings = PluginSettings(manager=lambda: manager, index=lambda: index, client=_Client)
    yield settings, manager, runner
    _settle(settings)


def _idle(settings):
    return settings.state()["busy"] == ""


def test_store_install_is_confirmed_before_pip_runs(plugins) -> None:
    settings, _manager, runner = plugins
    settings.load_store(False)
    _wait(lambda: settings.state()["store"]["loaded"])
    cards = {card["id"]: card for card in settings.state()["store"]["items"]}
    assert cards["io.example.demo"]["state"] == "install"
    assert cards["io.example.cal"]["state"] == "soon" and not cards["io.example.cal"]["installable"]
    settings.install_from_store("io.example.cal")  # coming soon: nothing happens
    assert _idle(settings)
    settings.install_from_store("io.example.demo")
    _wait(lambda: settings.state()["pending"] != {})
    rows = {row["label"]: row["value"] for row in settings.state()["pending"]["rows"]}
    assert rows["Source"] == URL and rows["Capabilities"] == "photos"
    assert not any("pip" in " ".join(call) for call in runner.calls)
    settings.confirm_install()
    _wait(lambda: _idle(settings) and settings.state()["plugins"])
    assert "Installed Demo photos v0.1.0" in settings.state()["message"]
    row = settings.state()["plugins"][0]
    assert row["managed"] and row["source"] == URL and row["state"] == "unconfigured"
    _wait(lambda: settings.state()["store"]["items"][0]["state"] == "installed")


def test_cancel_discards_the_checkout_and_errors_are_messages(plugins) -> None:
    settings, manager, _runner = plugins
    settings.prepare_install(URL, "")
    _wait(lambda: settings.state()["pending"] != {})
    settings.cancel_install()
    assert settings.state()["pending"] == {}
    _wait(lambda: not list((manager.root / "src").glob(".staging-*")))
    settings.prepare_install("http://insecure.example/x", "")
    _wait(lambda: settings.state()["message"] != "")
    assert settings.state()["messageOk"] is False and "https" in settings.state()["message"]


def test_settings_form_round_trip_and_enable_switch(plugins) -> None:
    settings, manager, _runner = plugins
    manager.commit(manager.prepare(URL))
    settings.load()
    _wait(lambda: settings.state()["loaded"] and _idle(settings))
    settings.load_config("io.example.demo")
    _wait(lambda: settings.state()["config"].get("loaded"))
    fields = {field["key"]: field for field in settings.state()["config"]["fields"]}
    assert fields["url"]["value"] == "https://a.example"
    settings.save_config("io.example.demo", {"url": "https://bad.example"})
    _wait(lambda: settings.state()["config"].get("errors"))
    assert settings.state()["config"]["errors"] == {"url": "not reachable"}
    settings.save_config("io.example.demo", {"url": "https://b.example"})
    _wait(lambda: settings.state()["config"] == {})
    assert _Client.saved[-1] == {"url": "https://b.example"}
    _wait(lambda: _idle(settings))
    manager.stopper.state = "stopped"
    settings.set_enabled("io.example.demo", False)
    _wait(lambda: _idle(settings) and settings.state()["plugins"][0]["enabled"] is False)
    assert manager.disabled() == {"io.example.demo"}
    assert settings.state()["message"] == "Stopped the running plugin."
    # A disabled plugin is never called: its form would start it again.
    settings.load_config("io.example.demo")
    assert settings.state()["config"] == {} and _idle(settings)
    settings.set_indexes(["http://nope"])
    assert settings.state()["messageOk"] is False


def test_update_and_remove_say_what_happened_to_the_running_plugin(plugins) -> None:
    settings, manager, runner = plugins
    manager.commit(manager.prepare(URL))
    runner.tags["v0.2.0"] = "b" * 40
    manager.stopper.state = "stopped"
    settings.prepare_update("io.example.demo")
    _wait(lambda: settings.state()["pending"] != {})
    settings.confirm_install()
    _wait(lambda: _idle(settings) and "Updated" in settings.state()["message"])
    assert settings.state()["message"] == (
        "Updated Demo photos to v0.2.0. Stopped the running plugin; it restarts on next use.")
    assert settings.state()["messageOk"] is True
    manager.stopper.state = "still-running"
    settings.remove("io.example.demo")
    _wait(lambda: _idle(settings) and "Removed" in settings.state()["message"])
    assert "(process 4242) did not exit" in settings.state()["message"]
    assert settings.state()["messageOk"] is False






class _GuidedClient:
    """A 1.3 plugin: test connection and a Nextcloud sign-in."""

    log: ClassVar[list[tuple]] = []
    polls: ClassVar[int] = 0

    def __init__(self, manifest) -> None:
        self.manifest = manifest

    def get_config(self):
        signed_in = _GuidedClient.polls >= 2
        return {"url": "https://cloud.example" if signed_in else "",
                "app_password": SECRET_MASK if signed_in else ""}

    def test_config(self, values):
        from blueferry.plugin_api.config_flow import ConfigTestResult

        _GuidedClient.log.append(("test", dict(values)))
        if values.get("url") == "https://down.example":
            return ConfigTestResult(False, "The server did not answer.", {"url": "unreachable"})
        return ConfigTestResult(True, "Connected as anna.")

    def config_login(self, values):
        from blueferry.plugin_api.config_flow import LoginStep

        _GuidedClient.log.append(("login", dict(values)))
        return LoginStep("open", login_id="flow-1", open_uri="https://cloud.example/login/v2/f")

    def config_login_status(self, login_id):
        from blueferry.plugin_api.config_flow import LoginStep

        _GuidedClient.polls += 1
        if _GuidedClient.polls < 2:
            return LoginStep("pending")
        return LoginStep("done", "Connected as anna")

    def config_login_cancel(self, login_id):
        _GuidedClient.log.append(("cancel", login_id))

    def status(self):
        return PluginStatus("ok")


GUIDED = """
[Config url]
Label=Server URL
Type=url
Required=true
Placeholder=https://cloud.example.com
HelpUrl=https://docs.example.com/url
Group=account

[Config app_password]
Label=App password
Type=secret
Required=true
Group=account

[Config timeout]
Label=Timeout
Type=int
Min=1
Max=60
Default=15
Advanced=true
"""


@pytest.fixture
def guided(app):
    from blueferry.plugin_api.testing import manifest

    plugin = manifest("io.example.cloud", capabilities="card;", api_version="1.3",
                      extra="ConfigTest=true\nConfigLogin=nextcloud\n" + GUIDED)
    opened: list[str] = []
    _GuidedClient.log, _GuidedClient.polls = [], 0
    settings = PluginSettings(manager=lambda: _NoManager(), index=lambda: None,
                              client=_GuidedClient, opener=opened.append, poll_ms=5)
    settings._manifests = {plugin.id: plugin}
    yield settings, opened
    _settle(settings)


def _settle(settings) -> None:
    """Let queued work finish before the next test: a task that reports to a
    collected PluginSettings later would crash a following event loop."""
    settle(settings)


class _NoManager:
    def entries(self):
        return [], []

    def settings(self):
        return {}


def test_guided_form_checks_tests_and_signs_in(guided) -> None:
    settings, opened = guided
    settings.load_config("io.example.cloud")
    _wait(lambda: settings.state()["config"].get("loaded"))
    config = settings.state()["config"]
    assert [g["name"] for g in config["groups"]] == ["account", "advanced"]
    assert config["actions"]["test"] and config["actions"]["loginLabel"] == "Sign in with Nextcloud"
    assert settings.check_config("io.example.cloud", {})["valid"] is False
    check = settings.check_config("io.example.cloud", {"url": "https://c.example",
                                                       "app_password": "x"})
    assert check["valid"] is True
    assert settings.check_config("other", {}) == {"errors": {}, "visible": [], "valid": False}
    settings.test_config("io.example.cloud", {"url": "https://down.example"})
    _wait(lambda: settings.state()["config"]["status"].get("pending") is False)
    status = settings.state()["config"]["status"]
    assert status == {"kind": "test", "ok": False, "text": "The server did not answer.",
                      "pending": False}
    assert settings.state()["config"]["errors"] == {"url": "unreachable"}
    settings.open_help("io.example.cloud", "url")
    settings.open_help("io.example.cloud", "app_password")
    assert opened == ["https://docs.example.com/url"]
    _wait(lambda: _idle(settings))
    settings.sign_in("io.example.cloud", {"url": "https://cloud.example", "app_password": "x"})
    _wait(lambda: settings.state()["config"]["status"].get("ok") is True
          and not settings.state()["config"]["status"]["pending"])
    assert opened[-1] == "https://cloud.example/login/v2/f"
    # The sign-in got the typed URL but never a secret.
    assert ("login", {"url": "https://cloud.example"}) in _GuidedClient.log
    _wait(lambda: settings.state()["config"]["fields"][0]["value"] == "https://cloud.example")
    assert settings.state()["config"]["status"]["text"] == "Connected as anna"


def test_closing_the_form_cancels_a_running_sign_in(guided) -> None:
    settings, _opened = guided
    _GuidedClient.polls = -10_000  # stays pending
    settings.load_config("io.example.cloud")
    _wait(lambda: settings.state()["config"].get("loaded"))
    settings.sign_in("io.example.cloud", {"url": "https://cloud.example"})
    _wait(lambda: settings._login is not None)
    settings.cancel_sign_in()
    assert settings.state()["config"]["status"]["text"] == "The sign-in was cancelled."
    _wait(lambda: ("cancel", "flow-1") in _GuidedClient.log)
    settings.sign_in("io.example.cloud", {"url": "https://cloud.example"})
    _wait(lambda: settings._login is not None)
    settings.close_config()
    assert settings._login is None and settings.state()["config"] == {}
    _wait(lambda: _GuidedClient.log.count(("cancel", "flow-1")) == 2)


def test_show_log_opens_the_plugin_log_or_explains(app, tmp_path, monkeypatch) -> None:
    from blueferry.plugin_api import logs as api_logs

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    opened: list[str] = []
    manager = PluginManager(data_home=tmp_path / "data", settings_path=tmp_path / "p.json",
                            runner=FakeRunner(), python="/usr/bin/python3",
                            stopper=FakeStopper())
    settings = PluginSettings(manager=lambda: manager, client=_Client, opener=opened.append,
                              index=lambda: PluginIndex(fetch=lambda _url: INDEX,
                                                        cache=tmp_path / "cache"))
    settings.open_log("io.example.ls")
    _wait(lambda: settings.state()["message"] != "")
    assert settings.state()["messageOk"] is False and "not written a log" in (
        settings.state()["message"])
    path = api_logs.log_path("io.example.ls")
    path.parent.mkdir(parents=True)
    path.write_text("started\n")
    settings.open_log("io.example.ls")
    _wait(lambda: bool(opened))
    assert opened == [path.as_uri()]
    _settle(settings)
