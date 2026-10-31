"""Qt plugin management, feature switches and the doctor: fakes only."""
from __future__ import annotations

import json
import threading
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


def test_removing_a_plugin_closes_its_settings_form(plugins) -> None:
    settings, manager, _runner = plugins
    manager.commit(manager.prepare(URL))
    settings.load()
    _wait(lambda: settings.state()["loaded"] and _idle(settings))
    settings.load_config("io.example.demo")
    _wait(lambda: settings.state()["config"].get("loaded"))
    settings.remove("io.example.demo")
    assert settings.state()["config"] == {}
    _wait(lambda: _idle(settings) and "Removed" in settings.state()["message"])
    assert settings.state()["config"] == {}






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


class _ActionClient:
    """A 1.6 plugin with two settings buttons."""

    ran: ClassVar[list[str]] = []
    port: ClassVar[int] = 47801

    def __init__(self, manifest) -> None:
        self.manifest = manifest

    def get_config(self):
        return {"port": _ActionClient.port}

    def status(self):
        return PluginStatus("ok")

    def run_config_action(self, action_id):
        from blueferry.plugin_api.client import PluginError
        from blueferry.plugin_api.surfaces import ActionResult

        _ActionClient.ran.append(action_id)
        if action_id == "setup_iphone":
            return ActionResult(True, None, "http://127.0.0.1:4711/abc/")
        if action_id == "broken":
            raise PluginError("the plugin did not answer in time")
        _ActionClient.port += 1
        return ActionResult(False, "No network to approve.")


ACTIONS = """
[Config port]
Label=Port
Type=int
HelpUrl=https://docs.example.com/port
HelpUrlLabel=Which port?

[ConfigAction setup_iphone]
Label=Set up <b>iPhone</b>
Icon=smartphone
Primary=true
Help=Shows a QR code.

[ConfigAction approve]
Label=Approve this network

[ConfigAction broken]
Label=Broken
"""


class _OneManager(_NoManager):
    def __init__(self, plugin) -> None:
        self.plugin = plugin

    def entries(self):
        from blueferry.plugin_manager import PluginEntry

        return [PluginEntry(self.plugin, None, True)], []


@pytest.fixture
def with_actions(app):
    from blueferry.plugin_api.testing import manifest

    plugin = manifest("io.example.setup", capabilities="card;", api_version="1.6",
                      extra=ACTIONS)
    opened: list[str] = []
    _ActionClient.ran, _ActionClient.port = [], 47801
    settings = PluginSettings(manager=lambda: _OneManager(plugin), index=lambda: None,
                              client=_ActionClient, opener=opened.append)
    settings._manifests = {plugin.id: plugin}
    yield settings, opened
    _settle(settings)


def test_settings_buttons_run_open_and_reload_the_form(with_actions) -> None:
    settings, opened = with_actions
    settings.load_config("io.example.setup")
    _wait(lambda: settings.state()["config"].get("loaded"))
    config = settings.state()["config"]
    assert config["actions"]["custom"] == [
        {"id": "setup_iphone", "label": "Set up &lt;b&gt;iPhone&lt;/b&gt;", "icon": "smartphone",
         "help": "Shows a QR code.", "primary": True},
        {"id": "approve", "label": "Approve this network", "icon": "", "help": "",
         "primary": False},
        {"id": "broken", "label": "Broken", "icon": "", "help": "", "primary": False},
    ]
    assert config["fields"][0]["helpUrlLabel"] == "Which port?"
    settings.run_action("io.example.setup", "setup_iphone")
    _wait(lambda: settings.state()["config"]["status"].get("pending") is False and _idle(settings))
    assert settings.state()["config"]["status"] == {
        "kind": "action", "ok": True, "text": "Done.", "pending": False}
    assert opened == ["http://127.0.0.1:4711/abc/"]
    settings.run_action("io.example.setup", "approve")
    _wait(lambda: settings.state()["config"]["fields"][0]["value"] == 47802)
    status = settings.state()["config"]["status"]
    assert (status["ok"], status["text"]) == (False, "No network to approve.")
    assert opened == ["http://127.0.0.1:4711/abc/"]  # nothing to open on failure
    _wait(lambda: _idle(settings))
    settings.run_action("io.example.setup", "broken")
    _wait(lambda: settings.state()["config"]["status"].get("pending") is False)
    assert settings.state()["config"]["status"]["text"] == "the plugin did not answer in time"
    # Undeclared ids, other forms and disabled plugins are never called.
    _wait(lambda: _idle(settings))
    settings.run_action("io.example.setup", "nope")
    settings.run_action("io.example.other", "setup_iphone")
    settings._disabled.add("io.example.setup")
    settings.run_action("io.example.setup", "setup_iphone")
    assert _ActionClient.ran == ["setup_iphone", "approve", "broken"]


def test_a_second_press_is_dropped_while_one_runs(with_actions) -> None:
    settings, _opened = with_actions
    settings.load_config("io.example.setup")
    _wait(lambda: settings.state()["config"].get("loaded"))
    settings._set_status("action", True, "Working…", pending=True)
    settings.run_action("io.example.setup", "approve")
    assert _ActionClient.ran == []


@pytest.mark.parametrize("reply", ["success", "failure", "error"])
def test_reopening_the_same_form_discards_old_action_results(with_actions, monkeypatch, reply):
    from blueferry.plugin_api.surfaces import ActionResult

    settings, opened = with_actions
    settings.load_config("io.example.setup")
    _wait(lambda: settings.state()["config"].get("loaded") and _idle(settings))
    started, release = threading.Event(), threading.Event()

    def run(_client, _action_id):
        started.set()
        assert release.wait(5)
        if reply == "error":
            raise RuntimeError("old failure")
        return ActionResult(reply == "success", "old answer", "https://example.org/old")

    monkeypatch.setattr(_ActionClient, "run_config_action", run)
    settings.run_action("io.example.setup", "setup_iphone")
    try:
        _wait(started.is_set)
        settings.close_config()
        settings.load_config("io.example.setup")
        statuses = []
        settings.changed.connect(lambda: statuses.append(settings.state()["config"]["status"]))
    finally:
        release.set()
    _wait(lambda: not settings._tasks and not settings._queue)
    assert settings.state()["config"]["loaded"] is True
    assert all(status == {} for status in statuses)
    assert opened == []


@pytest.mark.parametrize("leave", ["close", "disable", "reopen"])
def test_a_queued_action_never_calls_a_plugin_after_its_form_is_gone(with_actions, leave):
    settings, opened = with_actions
    settings.load_config("io.example.setup")
    _wait(lambda: settings.state()["config"].get("loaded") and _idle(settings))
    release = threading.Event()
    settings._work("list", lambda: release.wait(5), lambda _result: None)
    settings.run_action("io.example.setup", "setup_iphone")
    try:
        settings.close_config()
        if leave == "disable":
            settings._disabled.add("io.example.setup")
        elif leave == "reopen":
            settings.load_config("io.example.setup")
    finally:
        release.set()
    _wait(lambda: not settings._tasks and not settings._queue)
    assert _ActionClient.ran == []
    assert opened == []


def test_an_answer_never_lands_in_another_form(with_actions) -> None:
    from blueferry.plugin_api.testing import manifest

    settings, opened = with_actions
    other = manifest("io.example.other", capabilities="card;", api_version="1.6",
                     extra="[Config port]\nLabel=Port\nType=int\n")
    settings._manifests[other.id] = other
    settings.load_config("io.example.setup")
    _wait(lambda: settings.state()["config"].get("loaded"))
    settings.run_action("io.example.setup", "setup_iphone")
    # Finish the worker without dispatching its reply to the UI thread yet.
    # Otherwise the new execution guard can correctly skip this call entirely.
    assert settings._pool.waitForDone(5000)
    # The user switches to another plugin's form before the answer arrives.
    settings._update(config=settings._form(other, other.id, {}, loaded=True))
    _wait(lambda: _idle(settings) and _ActionClient.ran == ["setup_iphone"])
    _wait(lambda: _idle(settings))
    assert settings.state()["config"]["id"] == "io.example.other"
    assert settings.state()["config"]["status"] == {}
    assert opened == []  # nothing opens for a form that is no longer shown


def test_a_test_result_never_lands_in_another_or_a_closed_form(guided) -> None:
    from blueferry.plugin_api.testing import manifest

    settings, _opened = guided
    other = manifest("io.example.other", capabilities="card;", api_version="1.3",
                     extra="[Config port]\nLabel=Port\nType=int\n")
    settings._manifests[other.id] = other
    settings.load_config("io.example.cloud")
    _wait(lambda: settings.state()["config"].get("loaded"))
    settings.test_config("io.example.cloud", {"url": "https://down.example"})
    settings._update(config=settings._form(other, other.id, {}, loaded=True))
    _wait(lambda: _idle(settings))
    config = settings.state()["config"]
    assert config["id"] == "io.example.other"
    assert config["status"] == {} and config["errors"] == {}
    # Closed meanwhile: the answer does not bring a form back.
    settings.load_config("io.example.cloud")
    _wait(lambda: settings.state()["config"].get("loaded") and _idle(settings))
    settings.test_config("io.example.cloud", {"url": "https://down.example"})
    settings.close_config()
    _wait(lambda: _idle(settings))
    assert settings.state()["config"] == {}


class _GatedClient(_GuidedClient):
    """Holds GetConfig, SetConfig and ConfigLogin until the test releases them."""

    gate: ClassVar[threading.Event] = threading.Event()
    login_started: ClassVar[threading.Event] = threading.Event()

    def get_config(self):
        _GatedClient.gate.wait(5)
        return super().get_config()

    def set_config(self, values):
        _GatedClient.gate.wait(5)
        _GuidedClient.log.append(("save", dict(values)))
        if values.get("url") == "https://bad.example":
            return ConfigResult(False, {"url": "not reachable"})
        return ConfigResult(True, {})

    def config_login(self, values):
        _GatedClient.login_started.set()
        _GatedClient.gate.wait(5)
        return super().config_login(values)


@pytest.fixture
def gated(guided):
    from blueferry.plugin_api.testing import manifest

    settings, opened = guided
    settings._client = _GatedClient
    other = manifest("io.example.other", capabilities="card;", api_version="1.3",
                     extra=GUIDED)
    settings._manifests[other.id] = other
    _GatedClient.gate = threading.Event()
    _GatedClient.login_started = threading.Event()
    yield settings, opened
    _GatedClient.gate.set()


def test_a_late_config_answer_does_not_reopen_a_closed_form(gated) -> None:
    settings, _opened = gated
    settings.load_config("io.example.cloud")
    settings.close_config()
    _GatedClient.gate.set()
    _wait(lambda: _idle(settings))
    settings._pool.waitForDone()
    QCoreApplication.processEvents()
    assert settings.state()["config"] == {} and settings._stored == {}


def test_a_late_sign_in_opens_nothing_after_the_form_closed(gated) -> None:
    settings, opened = gated
    _GatedClient.gate.set()
    settings.load_config("io.example.cloud")
    _wait(lambda: settings.state()["config"].get("loaded"))
    _GatedClient.gate.clear()
    settings.sign_in("io.example.cloud", {"url": "https://cloud.example"})
    _wait(_GatedClient.login_started.is_set)
    settings.close_config()
    _GatedClient.gate.set()
    _wait(lambda: ("cancel", "flow-1") in _GuidedClient.log)
    assert opened == [] and settings._login is None and settings.state()["config"] == {}


@pytest.mark.parametrize("reply", ["open", "done", "error"])
def test_cancel_sign_in_before_initial_reply_discards_the_answer(gated, monkeypatch, reply):
    from blueferry.plugin_api.config_flow import LoginStep

    settings, opened = gated
    _GatedClient.gate.set()
    settings.load_config("io.example.cloud")
    _wait(lambda: settings.state()["config"].get("loaded") and _idle(settings))
    _GatedClient.gate.clear()

    def login(client, values):
        _GatedClient.login_started.set()
        _GatedClient.gate.wait(5)
        if reply == "error":
            raise RuntimeError("late failure")
        if reply == "done":
            return LoginStep("done", "late success")
        return _GuidedClient.config_login(client, values)

    monkeypatch.setattr(_GatedClient, "config_login", login)
    settings.sign_in("io.example.cloud", {"url": "https://cloud.example"})
    _wait(_GatedClient.login_started.is_set)
    settings.cancel_sign_in()
    cancelled = dict(settings.state()["config"]["status"])
    assert cancelled["text"] == "The sign-in was cancelled."
    assert cancelled["pending"] is False
    _GatedClient.gate.set()
    _wait(lambda: not settings._tasks)
    assert opened == [] and settings._login is None
    assert settings.state()["config"]["status"] == cancelled
    assert (("cancel", "flow-1") in _GuidedClient.log) is (reply == "open")


def test_cancelled_queued_sign_in_does_not_call_the_plugin(gated):
    settings, opened = gated
    _GatedClient.gate.set()
    settings.load_config("io.example.cloud")
    _wait(lambda: settings.state()["config"].get("loaded") and _idle(settings))
    _GatedClient.gate.clear()
    settings.test_config("io.example.cloud", {})
    # The test request owns the operation slot until its response is delivered.
    settings.sign_in("io.example.cloud", {"url": "https://cloud.example"})
    settings.cancel_sign_in()
    _GatedClient.gate.set()
    _wait(lambda: not settings._tasks and not settings._queue)
    assert opened == [] and not _GatedClient.login_started.is_set()


def test_reopening_the_same_form_does_not_revive_its_old_sign_in(gated):
    settings, opened = gated
    _GatedClient.gate.set()
    settings.load_config("io.example.cloud")
    _wait(lambda: settings.state()["config"].get("loaded") and _idle(settings))
    _GatedClient.gate.clear()
    settings.sign_in("io.example.cloud", {"url": "https://cloud.example"})
    _wait(_GatedClient.login_started.is_set)
    settings.close_config()
    settings.load_config("io.example.cloud")
    _GatedClient.gate.set()
    _wait(lambda: not settings._tasks and not settings._queue)
    assert opened == [] and settings._login is None
    assert settings.state()["config"]["status"] == {}
    assert ("cancel", "flow-1") in _GuidedClient.log


def test_a_new_sign_in_survives_the_cancelled_attempts_late_reply(gated):
    settings, opened = gated
    _GuidedClient.polls = -10000
    _GatedClient.gate.set()
    settings.load_config("io.example.cloud")
    _wait(lambda: settings.state()["config"].get("loaded") and _idle(settings))
    _GatedClient.gate.clear()
    settings.sign_in("io.example.cloud", {"url": "https://first.example"})
    _wait(_GatedClient.login_started.is_set)
    settings.cancel_sign_in()
    settings.sign_in("io.example.cloud", {"url": "https://second.example"})
    _GatedClient.gate.set()
    _wait(lambda: len(opened) == 1 and _idle(settings) and not settings._queue)
    assert opened == ["https://cloud.example/login/v2/f"]
    assert ("login", {"url": "https://second.example"}) in _GuidedClient.log
    assert _GuidedClient.log.count(("cancel", "flow-1")) == 1
    assert settings._login is not None
    assert settings.state()["config"]["status"]["pending"] is True


def test_a_save_that_ends_later_leaves_another_plugins_form_alone(gated) -> None:
    settings, _opened = gated
    _GatedClient.gate.set()
    settings.load_config("io.example.cloud")
    _wait(lambda: settings.state()["config"].get("loaded"))
    _GatedClient.gate.clear()
    settings.save_config("io.example.cloud", {"url": "https://bad.example", "app_password": "x"})
    settings.load_config("io.example.other")
    _GatedClient.gate.set()
    _wait(lambda: _idle(settings) and settings.state()["config"].get("loaded"))
    assert settings.state()["config"]["id"] == "io.example.other"
    assert settings.state()["config"]["errors"] == {}

    settings._reload = lambda: None  # keep the fake manifests
    _GatedClient.gate.clear()
    settings.save_config("io.example.other", {"url": "https://b.example", "app_password": "x"})
    settings.load_config("io.example.cloud")
    shown: list[object] = []
    settings.changed.connect(lambda: shown.append(settings.state()["config"].get("id")))
    _GatedClient.gate.set()
    _wait(lambda: _idle(settings) and settings.state()["config"].get("loaded"))
    assert any(entry[0] == "save" and entry[1]["url"] == "https://b.example"
               for entry in _GuidedClient.log)
    # The other plugin's success never closed the form shown meanwhile.
    assert set(shown) == {"io.example.cloud"}
