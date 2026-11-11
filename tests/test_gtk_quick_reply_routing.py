"""GTK: a popup click opens the quick reply box only when it is switched on."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.gtk_broadway import run_scenario


@pytest.mark.parametrize(
    "scenario", ["on", "off", "absent", "second-click", "no-handle", "existing-window"],
)
def test_gtk_quick_reply_routing(tmp_path, scenario):
    run_scenario(tmp_path, str(Path(__file__).resolve()), scenario)


def _exercise(scenario):
    from unittest.mock import patch

    from blueferry.quick_reply_state import QUICK_REPLY
    from blueferry.ui import app as app_module

    preferences = {
        "on": {QUICK_REPLY: True}, "second-click": {QUICK_REPLY: True},
        "off": {QUICK_REPLY: False}, "absent": {}, "no-handle": {QUICK_REPLY: True},
        "existing-window": {QUICK_REPLY: True},
    }[scenario]

    class FakeUiState:
        def __init__(self, _runner=None, _path=None):
            pass

        def load(self, done):
            done(dict(preferences))

    made = []

    class FakeBox:
        def __init__(self, *, application, client, handle, open_in_app, timer=None):
            self.application = application
            self.shown = [handle]
            self.open_in_app = open_in_app
            self.presented = 0
            made.append(self)

        def connect(self, *_args):
            return 1

        def set_startup_id(self, _token):
            pass

        def present(self):
            self.presented += 1

        def show_handle(self, handle):
            self.shown.append(handle)

    class FakeClient:
        def task_runner(self, _name):
            return None

    windows = []
    if scenario == "existing-window":
        _exercise_existing_window(app_module, FakeClient)
        return
    with (
        patch.object(app_module, "DaemonClient", FakeClient),
        patch.object(app_module, "UiState", FakeUiState),
        patch.object(app_module, "QuickReplyWindow", FakeBox),
        patch.object(app_module, "record_client_use", lambda _key: None),
        patch.object(app_module.BlueFerryApp, "_open_message",
                     lambda _self, handle, token, draft="": windows.append((handle, draft))),
    ):
        app = app_module.BlueFerryApp()
        if scenario == "no-handle":
            app._open_click("", "")
            assert windows == [("", "")] and made == []
            return
        app._open_click("h1", "")
        if scenario in ("off", "absent"):
            assert windows == [("h1", "")] and made == []
            return
        assert windows == [] and len(made) == 1
        box = made[0]
        assert box.shown == ["h1"] and box.presented == 1 and box.application is app
        box.open_in_app("h1", "half typed")
        assert windows == [("h1", "half typed")]
        if scenario == "second-click":
            app._open_click("h2", "")
            assert len(made) == 1
            assert box.shown == ["h1", "h2"]


def _exercise_existing_window(app_module, fake_client):
    """A box in front must not hide an open main window: no second one opens."""
    from unittest.mock import patch

    opened = []

    class FakeMain:
        def open_message(self, handle, draft=""):
            opened.append((handle, draft))

        def set_startup_id(self, _token):
            pass

    main = FakeMain()
    box = object()
    with (
        patch.object(app_module, "MainWindow", FakeMain),
        patch.object(app_module, "DaemonClient", fake_client),
        patch.object(app_module, "record_client_use", lambda _key: None),
    ):
        app = app_module.BlueFerryApp()
        app.get_active_window = lambda: box
        app.get_windows = lambda: [box, main]
        assert app._main_window() is main
        app._open_in_window("h1", "half typed")
        assert opened == [("h1", "half typed")]


if __name__ == "__main__":
    _exercise(sys.argv[1])
