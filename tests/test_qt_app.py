"""Qt process integration that does not construct a graphical application."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import types

import pytest

pytest.importorskip("PySide6")

from blueferry.qt import app as app_module

style_installed = app_module._quick_controls_style_installed


def test_focus_token_is_available_before_show_and_does_not_leak(monkeypatch):
    calls = []
    monkeypatch.setattr(app_module, "record_client_use", lambda *_args: None)

    class Window:
        def show(self):
            calls.append(("show", os.environ.get("XDG_ACTIVATION_TOKEN")))

        def raise_(self):
            pass

        def requestActivate(self):
            calls.append(("activate", os.environ.get("XDG_ACTIVATION_TOKEN")))

    app_module._present_window(Window(), "focus-token")
    app_module._present_window(Window())
    assert calls == [
        ("show", "focus-token"), ("activate", "focus-token"),
        ("show", None), ("activate", None),
    ]


STYLE_VARIABLES = (
    "QT_QUICK_CONTROLS_STYLE", "QT_STYLE_OVERRIDE", "DESKTOP_STARTUP_ID",
)


@pytest.fixture(autouse=True)
def isolated_style_environment(monkeypatch):
    """Let monkeypatch restore variables the code under test writes directly.

    delenv() on an unset variable records nothing to undo, so a value the
    application later puts into os.environ would outlive the test. setenv()
    first records the original state, including "unset".
    """
    for name in STYLE_VARIABLES:
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)


@pytest.fixture(autouse=True)
def kde_style_installed(monkeypatch):
    """Pretend qqc2-desktop-style is installed unless a test says otherwise."""
    installed = set(app_module.QUICK_CONTROLS_STYLES)
    monkeypatch.setattr(
        app_module, "_quick_controls_style_installed",
        lambda name: name in installed,
    )
    return installed


@pytest.fixture
def quick_style_binding(monkeypatch):
    """Install a stub PySide6.QtQuickControls2 and return the styles set."""
    styles = []
    binding = types.ModuleType("PySide6.QtQuickControls2")
    binding.QQuickStyle = types.SimpleNamespace(setStyle=styles.append)
    monkeypatch.setitem(sys.modules, "PySide6.QtQuickControls2", binding)
    return styles


@pytest.fixture
def no_quick_style_binding(monkeypatch):
    """Make importing PySide6.QtQuickControls2 fail as on Gentoo."""
    monkeypatch.setitem(sys.modules, "PySide6.QtQuickControls2", None)


def test_kde_quick_controls_style_is_set_without_the_binding(
        monkeypatch, no_quick_style_binding,
        ):
    app_module._select_quick_controls_style()
    assert os.environ["QT_QUICK_CONTROLS_STYLE"] == "org.kde.desktop"

    monkeypatch.setenv("QT_QUICK_CONTROLS_STYLE", "")
    app_module._select_quick_controls_style()
    assert os.environ["QT_QUICK_CONTROLS_STYLE"] == "org.kde.desktop"


def test_kde_quick_controls_style_uses_the_binding_when_available(
        quick_style_binding,
        ):
    app_module._select_quick_controls_style()

    assert quick_style_binding == ["org.kde.desktop"]
    assert "QT_QUICK_CONTROLS_STYLE" not in os.environ


def test_missing_kde_style_falls_back_to_fusion(
        kde_style_installed, quick_style_binding,
        ):
    kde_style_installed.discard("org.kde.desktop")

    app_module._select_quick_controls_style()

    assert quick_style_binding == ["Fusion"]


def test_no_installed_style_leaves_the_choice_to_qt(
        caplog, kde_style_installed, no_quick_style_binding,
        ):
    kde_style_installed.clear()

    with caplog.at_level("WARNING", logger=app_module.__name__):
        style = app_module._select_quick_controls_style(["-style", "fusion"])

    assert "QT_QUICK_CONTROLS_STYLE" not in os.environ
    assert style.qt_args == ["-style", "fusion"]
    assert len(caplog.records) == 1


def test_style_lookup_follows_qml_import_paths(monkeypatch, tmp_path):
    for name in ("QML_IMPORT_PATH", "QML2_IMPORT_PATH"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "org" / "example" / "style").mkdir(parents=True)
    (tmp_path / "org" / "example" / "style" / "qmldir").write_text("")
    (tmp_path / "QtQuick" / "Controls" / "Plain").mkdir(parents=True)
    (tmp_path / "QtQuick" / "Controls" / "Plain" / "qmldir").write_text("")

    assert not style_installed("org.example.style")
    monkeypatch.setenv("QML2_IMPORT_PATH", os.pathsep.join(["", str(tmp_path)]))
    assert style_installed("org.example.style")
    assert style_installed("Plain")
    assert not style_installed("org.example.missing")


def test_user_quick_controls_style_is_preserved(monkeypatch, quick_style_binding):
    monkeypatch.setenv("QT_QUICK_CONTROLS_STYLE", "Fusion")

    app_module._select_quick_controls_style()

    assert os.environ["QT_QUICK_CONTROLS_STYLE"] == "Fusion"
    assert quick_style_binding == []


def test_fallback_variable_is_not_inherited_after_qml_loaded(
        no_quick_style_binding,
        ):
    style = app_module._select_quick_controls_style()
    assert os.environ["QT_QUICK_CONTROLS_STYLE"] == "org.kde.desktop"

    style.release_environment()

    assert "QT_QUICK_CONTROLS_STYLE" not in os.environ


def test_user_quick_controls_style_survives_release(
        monkeypatch, no_quick_style_binding,
        ):
    monkeypatch.setenv("QT_QUICK_CONTROLS_STYLE", "Fusion")
    style = app_module._select_quick_controls_style()

    style.release_environment()

    assert os.environ["QT_QUICK_CONTROLS_STYLE"] == "Fusion"


@pytest.mark.parametrize(("qt_args", "remaining", "style"), [
    (["-style", "fusion", "-reverse"], ["-reverse"], "fusion"),
    (["-style=fusion"], [], "fusion"),
    (["--style=fusion"], [], "fusion"),
    (["-style", "a", "-style=b"], [], "b"),
    (["--style", "fusion"], ["--style", "fusion"], None),
    (["-style"], ["-style"], None),
])
def test_style_argument_forms_qt_honours_are_split_off(qt_args, remaining, style):
    assert app_module._split_style_argument(qt_args) == (remaining, style)


def _fake_main_collaborators(monkeypatch, events):
    """Replace Qt objects in main() so it runs without a display."""

    class Application:
        def __init__(self, argv) -> None:
            events.append(("application", argv, os.environ.get("QT_STYLE_OVERRIDE")))

        def setStyle(self, name) -> None:
            events.append(("widget style", name))

        def __getattr__(self, _name):
            return lambda *_args: None

    class Activation:
        primary = True

        def __init__(self, _application) -> None:
            pass

        def close(self) -> None:
            pass

    class Engine:
        def setInitialProperties(self, _properties) -> None:
            pass

        def load(self, _url) -> None:
            events.append(("load", os.environ.get("QT_QUICK_CONTROLS_STYLE")))

        def rootObjects(self):
            return []

    monkeypatch.setattr(app_module, "QApplication", Application)
    monkeypatch.setattr(
        app_module, "QIcon", types.SimpleNamespace(fromTheme=lambda _name: None),
    )
    monkeypatch.setattr(app_module, "_install_translation", lambda _application: None)
    monkeypatch.setattr(app_module, "ClientActivation", Activation)
    monkeypatch.setattr(app_module, "BridgeController", lambda *, parent: object())
    monkeypatch.setattr(app_module, "QQmlApplicationEngine", Engine)


def test_fallback_keeps_widget_style_choices_from_overriding_controls(
        monkeypatch, no_quick_style_binding,
        ):
    events = []
    _fake_main_collaborators(monkeypatch, events)
    monkeypatch.setattr(sys, "argv", ["blueferry-qt", "-style", "fusion"])
    monkeypatch.setenv("QT_STYLE_OVERRIDE", "kvantum")
    monkeypatch.setenv("DESKTOP_STARTUP_ID", "startup")

    assert app_module.main() == 1

    assert events == [
        ("application", ["blueferry-qt"], None),
        ("widget style", "fusion"),
        ("load", "org.kde.desktop"),
    ]
    assert os.environ["QT_STYLE_OVERRIDE"] == "kvantum"
    assert "QT_QUICK_CONTROLS_STYLE" not in os.environ


def test_binding_path_leaves_widget_style_choices_to_qt(
        monkeypatch, quick_style_binding,
        ):
    events = []
    _fake_main_collaborators(monkeypatch, events)
    monkeypatch.setattr(sys, "argv", ["blueferry-qt", "-style", "fusion"])
    monkeypatch.setenv("QT_STYLE_OVERRIDE", "kvantum")

    assert app_module.main() == 1

    assert quick_style_binding == ["org.kde.desktop"]
    assert events == [
        ("application", ["blueferry-qt", "-style", "fusion"], "kvantum"),
        ("load", None),
    ]


@pytest.mark.parametrize(("binding", "how"), [
    ("quick_style_binding", "QQuickStyle.setStyle()"),
    ("no_quick_style_binding", "QT_QUICK_CONTROLS_STYLE (no "),
])
def test_diagnose_style_reports_the_decision_without_a_window(
        monkeypatch, capsys, request, binding, how,
        ):
    request.getfixturevalue(binding)
    events = []
    _fake_main_collaborators(monkeypatch, events)
    monkeypatch.setattr(
        sys, "argv", ["blueferry-qt", "--diagnose-style", "-style", "fusion"],
    )

    assert app_module.main() == 0

    report = capsys.readouterr().out
    assert events == []
    assert "Controls style: org.kde.desktop" in report
    assert f"Chosen through: {how}" in report
    assert "QML import paths searched:" in report


def test_diagnose_style_names_a_user_style_and_a_missing_one(
        monkeypatch, kde_style_installed, no_quick_style_binding,
        ):
    monkeypatch.setenv("QT_QUICK_CONTROLS_STYLE", "Material")
    assert "Controls style: Material" in (
        app_module._select_quick_controls_style().describe()
    )

    monkeypatch.delenv("QT_QUICK_CONTROLS_STYLE")
    kde_style_installed.clear()
    report = app_module._select_quick_controls_style().describe()
    assert "Controls style: Qt default" in report
    assert "Chosen through: left to Qt: none of" in report


def test_fallback_style_loads_controls_despite_widget_style_override(tmp_path):
    """Run real Qt: -style and QT_STYLE_OVERRIDE must not break Main.qml."""
    pytest.importorskip("PySide6.QtQml")
    qml = tmp_path / "Probe.qml"
    qml.write_text("import QtQuick\nimport QtQuick.Controls\nButton {}\n")
    script = textwrap.dedent(f"""
        import os, sys
        sys.modules["PySide6.QtQuickControls2"] = None
        from PySide6.QtCore import QUrl
        from PySide6.QtQml import QQmlApplicationEngine
        from PySide6.QtWidgets import QApplication
        from blueferry.qt import app
        app.QUICK_CONTROLS_STYLES = ("org.example.missing", "Basic")
        style = app._select_quick_controls_style(["-style", "fusion"])
        application = QApplication(["probe", *style.qt_args])
        style.application_created(application)
        engine = QQmlApplicationEngine()
        engine.load(QUrl.fromLocalFile({str(qml)!r}))
        style.release_environment()
        print(bool(engine.rootObjects()), application.style().name())
    """)
    environment = {
        key: value for key, value in os.environ.items()
        if key not in ("QT_QUICK_CONTROLS_STYLE", "QT_QPA_PLATFORMTHEME")
    }
    environment.update(QT_QPA_PLATFORM="offscreen", QT_STYLE_OVERRIDE="fusion")
    result = subprocess.run(
        [sys.executable, "-c", script], env=environment,
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.stdout.split() == ["True", "fusion"], result.stderr


def test_missing_binding_falls_back_quietly(caplog, no_quick_style_binding):
    with caplog.at_level("DEBUG", logger=app_module.__name__):
        assert app_module._quick_style_binding() is None
    assert caplog.records == []


@pytest.mark.parametrize("error", [
    ImportError("libQt6QuickControls2.so.6: undefined symbol"),
    ModuleNotFoundError("No module named 'shiboken6'", name="shiboken6"),
])
def test_broken_binding_is_logged_before_falling_back(monkeypatch, caplog, error):
    import builtins

    real_import = builtins.__import__

    def failing_import(name, *args, **kwargs):
        if name == "PySide6.QtQuickControls2":
            raise error
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "PySide6.QtQuickControls2", raising=False)
    monkeypatch.setattr(builtins, "__import__", failing_import)
    with caplog.at_level("WARNING", logger=app_module.__name__):
        assert app_module._quick_style_binding() is None
    assert [record.exc_info[1] for record in caplog.records] == [error]


class _Signal:
    def __init__(self) -> None:
        self.callback = None

    def connect(self, callback) -> None:
        self.callback = callback

    def emit(self) -> None:
        if self.callback is not None:
            self.callback()


def test_terminal_signals_quit_qt_and_restore_previous_handlers(monkeypatch):
    class Application:
        def __init__(self) -> None:
            self.aboutToQuit = _Signal()
            self.quit_calls = 0

        def quit(self) -> None:
            self.quit_calls += 1

    class Timer:
        def __init__(self, parent) -> None:
            self.parent = parent
            self.timeout = _Signal()
            self.interval = 0
            self.started = False

        def setInterval(self, value: int) -> None:
            self.interval = value

        def start(self) -> None:
            self.started = True

    installed = {}
    previous = {
        signal.SIGINT: object(),
        signal.SIGTERM: object(),
    }
    monkeypatch.setattr(app_module, "QTimer", Timer)
    monkeypatch.setattr(
        app_module.signal,
        "getsignal",
        lambda signum: previous[signum],
    )
    monkeypatch.setattr(
        app_module.signal,
        "signal",
        lambda signum, handler: installed.__setitem__(signum, handler),
    )
    application = Application()

    timer = app_module._install_terminal_signal_handlers(application)

    assert timer.interval == 250
    assert timer.started is True
    installed[signal.SIGINT](signal.SIGINT, None)
    assert application.quit_calls == 1

    application.aboutToQuit.emit()
    assert installed == previous
