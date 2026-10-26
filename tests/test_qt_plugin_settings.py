"""Qt adapter of plugin management: pool, timer and signal wiring, fakes only.

The state machine itself is tested without Qt in
``tests/test_plugin_settings_model.py``; these tests run it on the real
``QThreadPool`` and ``QTimer`` of the adapter.
"""
from __future__ import annotations

import threading
from typing import ClassVar

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication
from PySide6.QtGui import QGuiApplication

from blueferry.plugin_index import PluginIndex
from blueferry.plugin_manager import PluginManager
from blueferry.qt.plugin_settings import PluginSettings
from tests.qt_settle import main_thread_gc, settle, wait
from tests.test_plugin_manager import URL, FakeRunner, FakeStopper
from tests.test_plugin_settings_model import GUIDED, INDEX, _Client, _GuidedClient, _NoManager

# Keep the cyclic GC off the pool threads (see tests/qt_settle.py).
pytestmark = pytest.mark.usefixtures(main_thread_gc.__name__)


@pytest.fixture(scope="module")
def app():
    return QCoreApplication.instance() or QGuiApplication([])


def _wait(predicate) -> None:
    wait(predicate)


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


@pytest.fixture
def guided(app):
    from blueferry.plugin_api.testing import manifest

    plugin = manifest("io.example.cloud", capabilities="card;", api_version="1.3",
                      extra="ConfigTest=true\nConfigLogin=nextcloud\n" + GUIDED)
    opened: list[str] = []
    _GuidedClient.log, _GuidedClient.polls = [], 0
    settings = PluginSettings(manager=lambda: _NoManager(), index=lambda: None,
                              client=_GuidedClient, opener=opened.append, poll_ms=5)
    settings.model._manifests = {plugin.id: plugin}
    yield settings, opened
    _settle(settings)


def _settle(settings) -> None:
    """Let queued work finish before the next test: a task that reports to a
    collected PluginSettings later would crash a following event loop."""
    settle(settings)


def test_closing_the_form_cancels_a_running_sign_in(guided) -> None:
    settings, _opened = guided
    _GuidedClient.polls = -10_000  # stays pending
    settings.load_config("io.example.cloud")
    _wait(lambda: settings.state()["config"].get("loaded"))
    settings.sign_in("io.example.cloud", {"url": "https://cloud.example"})
    _wait(lambda: settings.model._login is not None)
    settings.cancel_sign_in()
    assert settings.state()["config"]["status"]["text"] == "The sign-in was cancelled."
    _wait(lambda: ("cancel", "flow-1") in _GuidedClient.log)
    settings.sign_in("io.example.cloud", {"url": "https://cloud.example"})
    _wait(lambda: settings.model._login is not None)
    settings.close_config()
    assert settings.model._login is None and settings.state()["config"] == {}
    _wait(lambda: _GuidedClient.log.count(("cancel", "flow-1")) == 2)


class _GatedClient(_GuidedClient):
    """Holds GetConfig and ConfigLogin on the pool until the test releases them."""

    gate: ClassVar[threading.Event] = threading.Event()

    def get_config(self):
        _GatedClient.gate.wait(5)
        return super().get_config()

    def config_login(self, values):
        _GatedClient.gate.wait(5)
        return super().config_login(values)


@pytest.fixture
def gated(guided):
    from blueferry.plugin_api.testing import manifest

    settings, opened = guided
    settings.model._client = _GatedClient
    other = manifest("io.example.other", capabilities="card;", api_version="1.3",
                     extra=GUIDED)
    settings.model._manifests[other.id] = other
    _GatedClient.gate = threading.Event()
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
    assert settings.state()["config"] == {} and settings.model._stored == {}


def test_a_late_sign_in_opens_nothing_after_the_form_closed(gated) -> None:
    settings, opened = gated
    _GatedClient.gate.set()
    settings.load_config("io.example.cloud")
    _wait(lambda: settings.state()["config"].get("loaded"))
    _GatedClient.gate.clear()
    settings.sign_in("io.example.cloud", {"url": "https://cloud.example"})
    settings.close_config()
    _GatedClient.gate.set()
    _wait(lambda: ("cancel", "flow-1") in _GuidedClient.log)
    assert opened == [] and settings.model._login is None and settings.state()["config"] == {}


def test_sign_in_polls_on_the_qt_timer_and_reports_changes(guided) -> None:
    settings, opened = guided
    shown: list[str] = []
    settings.changed.connect(lambda: shown.append(settings.state()["busy"]))
    settings.load_config("io.example.cloud")
    _wait(lambda: settings.state()["config"].get("loaded"))
    assert "config" in shown and shown[-1] == ""
    settings.sign_in("io.example.cloud", {"url": "https://cloud.example", "app_password": "x"})
    _wait(lambda: settings.state()["config"]["status"].get("ok") is True
          and not settings.state()["config"]["status"]["pending"])
    assert opened[-1] == "https://cloud.example/login/v2/f"
    _wait(lambda: settings.state()["config"]["fields"][0]["value"] == "https://cloud.example")
    assert settings.state()["config"]["status"]["text"] == "Connected as anna"
