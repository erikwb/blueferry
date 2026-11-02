"""Plugin management state machine shared by the clients: no toolkit, fakes only.

Work is queued on a :class:`tests.client_fakes.ManualRunner` and runs only
when the test drains it, so late answers are produced by simply holding the
work back.
"""
from __future__ import annotations

from typing import ClassVar

import pytest

from blueferry import plugin_settings_view as view
from blueferry.plugin_api.client import PluginStatus
from blueferry.plugin_index import PluginIndex
from blueferry.plugin_manager import PluginManager
from blueferry.plugin_settings_model import PluginSettingsModel
from tests.client_fakes import ManualRunner
from tests.plugin_settings_fakes import GUIDED, INDEX, DemoClient, GuidedClient, NoManager
from tests.test_plugin_manager import URL, FakeRunner, FakeStopper


def _model(runner: ManualRunner, opened: list[str] | None = None, **kwargs) -> PluginSettingsModel:
    changes: list[None] = []
    model = PluginSettingsModel(
        runner=runner, timer=runner.timer,
        opener=(opened if opened is not None else []).append,
        on_change=lambda: changes.append(None), **kwargs,
    )
    model.changes = changes  # type: ignore[attr-defined]
    return model


@pytest.fixture
def plugins(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    runner = FakeRunner()
    manager = PluginManager(data_home=tmp_path / "data", settings_path=tmp_path / "p.json",
                            runner=runner, python="/usr/bin/python3",
                            stopper=FakeStopper())
    index = PluginIndex(fetch=lambda _url: INDEX, cache=tmp_path / "cache")
    DemoClient.saved = []
    work = ManualRunner()
    settings = _model(work, manager=lambda: manager, index=lambda: index, client=DemoClient)
    return settings, manager, runner, work


def _idle(settings):
    return settings.state()["busy"] == ""


def test_every_change_is_reported_and_the_state_is_a_copy(plugins) -> None:
    settings, _manager, _runner, work = plugins
    settings.load_store(False)
    assert settings.state()["busy"] == "store" and settings.changes
    copy = settings.state()
    copy["busy"] = "tampered"
    assert settings.state()["busy"] == "store"
    work.drain()
    assert settings.state()["store"]["loaded"] and _idle(settings)


def test_store_install_is_confirmed_before_pip_runs(plugins) -> None:
    settings, _manager, runner, work = plugins
    settings.load_store(False)
    work.drain()
    cards = {card["id"]: card for card in settings.state()["store"]["items"]}
    assert cards["io.example.demo"]["state"] == "install"
    assert cards["io.example.cal"]["state"] == "soon" and not cards["io.example.cal"]["installable"]
    settings.install_from_store("io.example.cal")  # coming soon: nothing happens
    assert _idle(settings) and not work.pending
    settings.install_from_store("io.example.demo")
    work.drain()
    rows = {row["label"]: row["value"] for row in settings.state()["pending"]["rows"]}
    assert rows["Source"] == URL and rows["Capabilities"] == "photos"
    assert not any("pip" in call for call in runner.calls)
    settings.confirm_install()
    work.drain()
    assert "Installed Demo photos v0.1.0" in settings.state()["message"]
    row = settings.state()["plugins"][0]
    assert row["managed"] and row["source"] == URL and row["state"] == "unconfigured"
    assert settings.state()["store"]["items"][0]["state"] == "installed"


def test_steps_wait_for_the_running_one(plugins) -> None:
    settings, _manager, _runner, work = plugins
    settings.load()
    settings.load_store(False)
    assert settings.state()["busy"] == "list" and len(work.pending) == 1
    work.run_next()
    assert settings.state()["busy"] == "store" and len(work.pending) == 1
    work.drain()
    assert _idle(settings) and settings.state()["loaded"] and settings.state()["store"]["loaded"]


def test_cancel_discards_the_checkout_and_errors_are_messages(plugins) -> None:
    settings, manager, _runner, work = plugins
    settings.prepare_install(URL, "")
    work.drain()
    assert settings.state()["pending"] != {}
    settings.cancel_install()
    assert settings.state()["pending"] == {}
    assert list((manager.root / "src").glob(".staging-*"))  # removed off the UI thread
    work.drain()
    assert not list((manager.root / "src").glob(".staging-*"))
    settings.prepare_install("http://insecure.example/x", "")
    work.drain()
    assert settings.state()["messageOk"] is False and "https" in settings.state()["message"]


def test_settings_form_round_trip_and_enable_switch(plugins) -> None:
    settings, manager, _runner, work = plugins
    manager.commit(manager.prepare(URL))
    settings.load()
    work.drain()
    settings.load_config("io.example.demo")
    assert settings.state()["config"]["loaded"] is False  # shown at once, values follow
    work.drain()
    fields = {field["key"]: field for field in settings.state()["config"]["fields"]}
    assert fields["url"]["value"] == "https://a.example"
    settings.save_config("io.example.demo", {"url": "https://bad.example"})
    work.drain()
    assert settings.state()["config"]["errors"] == {"url": "not reachable"}
    settings.save_config("io.example.demo", {"url": "https://b.example"})
    work.drain()
    assert settings.state()["config"] == {}
    assert DemoClient.saved[-1] == {"url": "https://b.example"}
    manager.stopper.state = "stopped"
    settings.set_enabled("io.example.demo", False)
    work.drain()
    assert settings.state()["plugins"][0]["enabled"] is False
    assert manager.disabled() == {"io.example.demo"}
    assert settings.state()["message"] == "Stopped the running plugin."
    # A disabled plugin is never called: its form would start it again.
    settings.load_config("io.example.demo")
    assert settings.state()["config"] == {} and _idle(settings) and not work.pending
    settings.set_indexes(["http://nope"])
    assert settings.state()["messageOk"] is False


def test_update_and_remove_say_what_happened_to_the_running_plugin(plugins) -> None:
    settings, manager, runner, work = plugins
    manager.commit(manager.prepare(URL))
    runner.tags["v0.2.0"] = "b" * 40
    manager.stopper.state = "stopped"
    settings.prepare_update("io.example.demo")
    work.drain()
    assert settings.state()["pending"]["kind"] == "update"
    settings.confirm_install()
    work.drain()
    assert settings.state()["message"] == (
        "Updated Demo photos to v0.2.0. Stopped the running plugin; it restarts on next use.")
    assert settings.state()["messageOk"] is True
    manager.stopper.state = "still-running"
    settings.remove("io.example.demo")
    work.drain()
    assert "(process 4242) did not exit" in settings.state()["message"]
    assert settings.state()["messageOk"] is False


def test_removing_a_plugin_closes_its_settings_form(plugins) -> None:
    settings, manager, _runner, work = plugins
    manager.commit(manager.prepare(URL))
    settings.load()
    work.drain()
    settings.load_config("io.example.demo")
    work.drain()
    assert settings.state()["config"]["loaded"]
    settings.remove("io.example.demo")
    assert settings.state()["config"] == {}
    work.drain()
    assert "Removed" in settings.state()["message"] and settings.state()["config"] == {}


@pytest.fixture
def guided():
    from blueferry.plugin_api.testing import manifest

    plugin = manifest("io.example.cloud", capabilities="card;", api_version="1.3",
                      extra="ConfigTest=true\nConfigLogin=nextcloud\n" + GUIDED)
    other = manifest("io.example.other", capabilities="card;", api_version="1.3",
                     extra=GUIDED)
    opened: list[str] = []
    GuidedClient.log, GuidedClient.polls = [], 0
    work = ManualRunner()
    settings = _model(work, opened, manager=lambda: NoManager(), index=lambda: None,
                      client=GuidedClient, poll_ms=5)
    settings._manifests = {plugin.id: plugin, other.id: other}
    return settings, opened, work


def test_guided_form_checks_tests_and_signs_in(guided) -> None:
    settings, opened, work = guided
    settings.load_config("io.example.cloud")
    work.drain()
    config = settings.state()["config"]
    assert [g["name"] for g in config["groups"]] == ["account", "advanced"]
    assert config["actions"]["test"] and config["actions"]["loginLabel"] == "Sign in with Nextcloud"
    assert settings.check_config("io.example.cloud", {})["valid"] is False
    check = settings.check_config("io.example.cloud", {"url": "https://c.example",
                                                       "app_password": "x"})
    assert check["valid"] is True
    assert settings.check_config("other", {}) == {"errors": {}, "visible": [], "valid": False}
    settings.test_config("io.example.cloud", {"url": "https://down.example"})
    assert settings.state()["config"]["status"]["pending"] is True
    work.drain()
    status = settings.state()["config"]["status"]
    assert status == {"kind": "test", "ok": False, "text": "The server did not answer.",
                      "pending": False}
    assert settings.state()["config"]["errors"] == {"url": "unreachable"}
    settings.open_help("io.example.cloud", "url")
    settings.open_help("io.example.cloud", "app_password")
    assert opened == ["https://docs.example.com/url"]
    settings.sign_in("io.example.cloud", {"url": "https://cloud.example", "app_password": "x"})
    work.run_tasks()
    assert opened[-1] == "https://cloud.example/login/v2/f"
    timer = settings._login_timer
    assert timer.delay == 5 and settings.state()["config"]["status"]["pending"] is True
    work.drain()
    # The sign-in got the typed URL but never a secret.
    assert ("login", {"url": "https://cloud.example"}) in GuidedClient.log
    assert settings.state()["config"]["fields"][0]["value"] == "https://cloud.example"
    assert settings.state()["config"]["status"] == {
        "kind": "login", "ok": True, "text": "Connected as anna", "pending": False}


def test_a_sign_in_that_never_ends_expires(guided) -> None:
    settings, _opened, work = guided
    GuidedClient.polls = -10_000  # stays pending
    settings._poll_ms = 600_000  # LOGIN_TIMEOUT_SECONDS allows two polls
    settings.load_config("io.example.cloud")
    work.drain()
    settings.sign_in("io.example.cloud", {"url": "https://cloud.example"})
    for _round in range(20):
        work.run_tasks()
        if not work.fire_timers():
            break
    assert settings._login is None and GuidedClient.polls == -10_000 + 2
    assert settings.state()["config"]["status"] == {
        "kind": "login", "ok": False, "text": view.login_text("expired"), "pending": False}


def test_closing_the_form_cancels_a_running_sign_in(guided) -> None:
    settings, _opened, work = guided
    GuidedClient.polls = -10_000  # stays pending
    settings.load_config("io.example.cloud")
    work.drain()
    settings.sign_in("io.example.cloud", {"url": "https://cloud.example"})
    work.run_tasks()
    assert settings._login is not None
    settings.cancel_sign_in()
    assert settings.state()["config"]["status"]["text"] == "The sign-in was cancelled."
    assert not settings._login_timer.active
    work.run_tasks()
    assert ("cancel", "flow-1") in GuidedClient.log
    settings.sign_in("io.example.cloud", {"url": "https://cloud.example"})
    work.run_tasks()
    assert settings._login is not None
    settings.close_config()
    assert settings._login is None and settings.state()["config"] == {}
    work.run_tasks()
    assert GuidedClient.log.count(("cancel", "flow-1")) == 2


def test_a_poll_answer_for_a_cancelled_sign_in_is_dropped(guided) -> None:
    settings, _opened, work = guided
    settings.load_config("io.example.cloud")
    work.drain()
    settings.sign_in("io.example.cloud", {"url": "https://cloud.example"})
    work.run_tasks()
    GuidedClient.polls = 5  # the next poll says "done"
    settings._login_timer.fire()  # the poll is queued ...
    settings.cancel_sign_in()  # ... and the user cancels before it answers
    work.drain()
    assert settings.state()["config"]["status"]["text"] == "The sign-in was cancelled."


def test_show_log_opens_the_plugin_log_or_explains(tmp_path, monkeypatch) -> None:
    from blueferry.plugin_api import logs as api_logs

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    opened: list[str] = []
    manager = PluginManager(data_home=tmp_path / "data", settings_path=tmp_path / "p.json",
                            runner=FakeRunner(), python="/usr/bin/python3",
                            stopper=FakeStopper())
    work = ManualRunner()
    settings = _model(work, opened, manager=lambda: manager, client=DemoClient,
                      index=lambda: PluginIndex(fetch=lambda _url: INDEX,
                                                cache=tmp_path / "cache"))
    settings.open_log("io.example.ls")
    work.drain()
    assert settings.state()["messageOk"] is False and "not written a log" in (
        settings.state()["message"])
    path = api_logs.log_path("io.example.ls")
    path.parent.mkdir(parents=True)
    path.write_text("started\n")
    settings.open_log("io.example.ls")
    work.drain()
    assert opened == [path.as_uri()]


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


class _OneManager(NoManager):
    def __init__(self, plugin) -> None:
        self.plugin = plugin

    def entries(self):
        from blueferry.plugin_manager import PluginEntry

        return [PluginEntry(self.plugin, None, True)], []

    def set_enabled(self, plugin_id, enabled):
        return None  # nothing was running


@pytest.fixture
def with_actions():
    from blueferry.plugin_api.testing import manifest

    plugin = manifest("io.example.setup", capabilities="card;", api_version="1.6",
                      extra=ACTIONS)
    opened: list[str] = []
    _ActionClient.ran, _ActionClient.port = [], 47801
    work = ManualRunner()
    settings = _model(work, opened, manager=lambda: _OneManager(plugin), index=lambda: None,
                      client=_ActionClient)
    settings._manifests = {plugin.id: plugin}
    return settings, opened, work


def test_settings_buttons_run_open_and_reload_the_form(with_actions) -> None:
    settings, opened, work = with_actions
    settings.load_config("io.example.setup")
    work.drain()
    config = settings.state()["config"]
    assert config["actions"]["custom"] == [
        {"id": "setup_iphone", "label": "Set up &lt;b&gt;iPhone&lt;/b&gt;",
         "labelPlain": "Set up <b>iPhone</b>", "icon": "smartphone",
         "help": "Shows a QR code.", "primary": True},
        {"id": "approve", "label": "Approve this network",
         "labelPlain": "Approve this network", "icon": "", "help": "", "primary": False},
        {"id": "broken", "label": "Broken", "labelPlain": "Broken", "icon": "", "help": "",
         "primary": False},
    ]
    assert config["fields"][0]["helpUrlLabel"] == "Which port?"
    assert config["fields"][0]["helpUrlLabelPlain"] == "Which port?"
    settings.run_action("io.example.setup", "setup_iphone")
    assert settings.state()["config"]["status"] == {
        "kind": "action", "ok": True, "text": "Working…", "pending": True}
    work.drain()
    assert settings.state()["config"]["status"] == {
        "kind": "action", "ok": True, "text": "Done.", "pending": False}
    assert opened == ["http://127.0.0.1:4711/abc/"]
    settings.run_action("io.example.setup", "approve")
    work.drain()
    assert settings.state()["config"]["fields"][0]["value"] == 47802
    status = settings.state()["config"]["status"]
    assert (status["ok"], status["text"]) == (False, "No network to approve.")
    assert opened == ["http://127.0.0.1:4711/abc/"]  # nothing to open on failure
    settings.run_action("io.example.setup", "broken")
    work.drain()
    assert settings.state()["config"]["status"]["text"] == "the plugin did not answer in time"
    # Undeclared ids, other forms and disabled plugins are never called.
    settings.run_action("io.example.setup", "nope")
    settings.run_action("io.example.other", "setup_iphone")
    settings._disabled.add("io.example.setup")
    settings.run_action("io.example.setup", "setup_iphone")
    work.drain()
    assert _ActionClient.ran == ["setup_iphone", "approve", "broken"]


def test_a_second_press_is_dropped_while_one_runs(with_actions) -> None:
    settings, _opened, work = with_actions
    settings.load_config("io.example.setup")
    work.drain()
    settings.run_action("io.example.setup", "approve")
    settings.run_action("io.example.setup", "approve")
    work.drain()
    assert _ActionClient.ran == ["approve"]


def test_queued_action_is_cancelled_when_form_closes(with_actions):
    settings, opened, work = with_actions
    settings.load_config("io.example.setup")
    work.drain()
    settings.run_action("io.example.setup", "setup_iphone")
    settings.close_config()
    work.drain()
    assert _ActionClient.ran == [] and opened == []


def test_same_plugin_reopened_form_rejects_old_action_reply(with_actions):
    settings, opened, work = with_actions
    settings.load_config("io.example.setup")
    work.drain()
    settings.run_action("io.example.setup", "setup_iphone")
    reply = work.start_next()
    settings.close_config()
    settings.load_config("io.example.setup")
    reply()
    work.drain()
    assert opened == [] and settings.state()["config"]["status"] == {}


def test_failed_settings_refresh_disables_form_until_read_retry(with_actions, monkeypatch):
    settings, _opened, work = with_actions
    settings.load_config("io.example.setup")
    work.drain()
    settings.run_action("io.example.setup", "approve")
    get_config = _ActionClient.get_config
    monkeypatch.setattr(_ActionClient, "get_config",
                        lambda _self: (_ for _ in ()).throw(RuntimeError("read failed")))
    work.drain()
    assert _ActionClient.port == 47802
    config = settings.state()["config"]
    assert not config["loaded"] and config["loadError"] == "read failed"
    settings.run_action("io.example.setup", "approve")
    settings.save_config("io.example.setup", {"port": 47801})
    assert not work.pending and _ActionClient.ran == ["approve"]
    monkeypatch.setattr(_ActionClient, "get_config", get_config)
    settings.load_config("io.example.setup")
    work.drain()
    assert settings.state()["config"]["loaded"]
    assert settings.state()["config"]["fields"][0]["value"] == 47802
    assert _ActionClient.ran == ["approve"]


def test_an_answer_never_lands_in_another_form(with_actions) -> None:
    from blueferry.plugin_api.testing import manifest

    settings, opened, work = with_actions
    other = manifest("io.example.other", capabilities="card;", api_version="1.6",
                     extra="[Config port]\nLabel=Port\nType=int\n")
    settings._manifests[other.id] = other
    settings.load_config("io.example.setup")
    work.drain()
    settings.run_action("io.example.setup", "setup_iphone")
    # The user switches to another plugin's form before the answer arrives.
    reply = work.start_next()
    settings._update(config=settings._form(other, other.id, {}, loaded=True))
    reply()
    work.drain()
    assert _ActionClient.ran == ["setup_iphone"]
    assert settings.state()["config"]["id"] == "io.example.other"
    assert settings.state()["config"]["status"] == {}
    assert opened == []  # nothing opens for a form that is no longer shown


def test_an_action_of_a_plugin_disabled_meanwhile_opens_nothing(with_actions) -> None:
    settings, opened, work = with_actions
    settings.load_config("io.example.setup")
    work.drain()
    settings.run_action("io.example.setup", "setup_iphone")
    reply = work.start_next()
    settings.set_enabled("io.example.setup", False)  # the kill switch closes the form
    reply()
    work.drain()
    assert _ActionClient.ran == ["setup_iphone"] and opened == []
    assert settings.state()["config"] == {}


def test_a_test_result_never_lands_in_another_or_a_closed_form(guided) -> None:
    from blueferry.plugin_api.testing import manifest

    settings, _opened, work = guided
    other = manifest("io.example.plain", capabilities="card;", api_version="1.3",
                     extra="[Config port]\nLabel=Port\nType=int\n")
    settings._manifests[other.id] = other
    settings.load_config("io.example.cloud")
    work.drain()
    settings.test_config("io.example.cloud", {"url": "https://down.example"})
    settings._update(config=settings._form(other, other.id, {}, loaded=True))
    work.drain()
    config = settings.state()["config"]
    assert config["id"] == "io.example.plain"
    assert config["status"] == {} and config["errors"] == {}
    # Closed meanwhile: the answer does not bring a form back.
    settings.load_config("io.example.cloud")
    work.drain()
    settings.test_config("io.example.cloud", {"url": "https://down.example"})
    settings.close_config()
    work.drain()
    assert settings.state()["config"] == {}


def test_a_late_config_answer_does_not_reopen_a_closed_form(guided) -> None:
    settings, _opened, work = guided
    settings.load_config("io.example.cloud")
    settings.close_config()
    work.drain()
    assert settings.state()["config"] == {} and settings._stored == {}


def test_a_late_config_answer_does_not_replace_a_newer_form(guided) -> None:
    settings, _opened, work = guided
    settings.load_config("io.example.cloud")
    settings.load_config("io.example.other")
    GuidedClient.polls = 2  # the answers differ from now on
    work.run_next()  # the first form's answer: dropped
    assert settings.state()["config"]["id"] == "io.example.other"
    assert settings.state()["config"]["loaded"] is False
    work.drain()
    assert settings.state()["config"]["loaded"] is True
    assert settings.state()["config"]["id"] == "io.example.other"


def test_a_late_sign_in_opens_nothing_after_the_form_closed(guided) -> None:
    settings, opened, work = guided
    settings.load_config("io.example.cloud")
    work.drain()
    settings.sign_in("io.example.cloud", {"url": "https://cloud.example"})
    reply = work.start_next()
    settings.close_config()
    reply()
    work.drain()
    assert ("cancel", "flow-1") in GuidedClient.log
    assert opened == [] and settings._login is None and settings.state()["config"] == {}


@pytest.mark.parametrize("started", [False, True])
def test_cancel_sign_in_before_initial_reply(guided, started):
    settings, opened, work = guided
    settings.load_config("io.example.cloud")
    work.drain()
    settings.sign_in("io.example.cloud", {"url": "https://cloud.example"})
    reply = work.start_next() if started else None
    settings.cancel_sign_in()
    if reply is not None:
        reply()
    work.drain()
    assert opened == [] and settings._login is None and not settings._login_timer.active
    assert (("cancel", "flow-1") in GuidedClient.log) is started
    if not started:
        assert GuidedClient.log == []


def test_a_save_that_ends_later_leaves_another_plugins_form_alone(guided) -> None:
    settings, _opened, work = guided
    settings.load_config("io.example.cloud")
    work.drain()
    settings.save_config("io.example.cloud", {"url": "https://bad.example", "app_password": "x"})
    settings.load_config("io.example.other")
    work.drain()
    assert settings.state()["config"]["id"] == "io.example.other"
    assert settings.state()["config"]["loaded"] and settings.state()["config"]["errors"] == {}

    settings._reload = lambda: None  # keep the fake manifests
    settings.save_config("io.example.other", {"url": "https://b.example", "app_password": "x"})
    settings.load_config("io.example.cloud")
    shown: list[object] = []
    settings._on_change = lambda: shown.append(settings.state()["config"].get("id"))
    work.drain()
    assert settings.state()["config"]["loaded"]
    assert any(entry[0] == "save" and entry[1]["url"] == "https://b.example"
               for entry in GuidedClient.log)
    # The other plugin's success never closed the form shown meanwhile.
    assert set(shown) == {"io.example.cloud"}


def test_failed_step_names_only_a_step_that_raised() -> None:
    from blueferry.plugin_api.testing import manifest
    from blueferry.plugin_manager import InstallError
    from blueferry.plugin_stopper import STILL_RUNNING, StopOutcome

    plugin = manifest("io.example.setup", capabilities="card;", api_version="1.6")
    answers: list = [InstallError("settings file is read-only"),
                     StopOutcome(STILL_RUNNING, 4242)]

    class Manager(_OneManager):
        def set_enabled(self, plugin_id, enabled):
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

    work = ManualRunner()
    settings = _model(work, manager=lambda: Manager(plugin), index=lambda: None,
                      client=_ActionClient)
    assert settings.state()["failedStep"] == ""
    settings.set_enabled("io.example.setup", False)
    work.run_tasks()
    assert settings.state()["failedStep"] == "enable"
    assert "read-only" in settings.state()["message"]
    # The next step clears it; a plugin that keeps running is a warning only.
    settings.set_enabled("io.example.setup", False)
    assert settings.state()["failedStep"] == ""
    work.run_tasks()
    assert settings.state()["messageOk"] is False and settings.state()["message"]
    assert settings.state()["failedStep"] == ""
