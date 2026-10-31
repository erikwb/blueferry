"""Qt feature switches and the in-app doctor report: fakes only."""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer
from PySide6.QtGui import QGuiApplication

from blueferry.doctor_report import DoctorReport


@pytest.fixture(scope="module")
def app():
    return QCoreApplication.instance() or QGuiApplication([])


def _wait(predicate) -> None:
    loop = QEventLoop()
    timer = QTimer()
    timer.timeout.connect(lambda: loop.quit() if predicate() else None)
    timer.start(10)
    # An owned timer, stopped below: a QTimer.singleShot(…, loop.quit) would
    # fire seconds later on this loop after it is gone and crash the suite.
    deadline = QTimer()
    deadline.setSingleShot(True)
    deadline.timeout.connect(loop.quit)
    deadline.start(5000)
    loop.exec()
    timer.stop()
    deadline.stop()


def test_controller_features_and_doctor(app, monkeypatch) -> None:
    from blueferry.qt.controller import BridgeController
    from tests.test_qt_controller import _Backend

    class Backend(_Backend):
        def features(self):
            return {"mark_read_on_dismiss": {"value": False, "restart_required": True}}

        def set_feature(self, name, enabled):
            self.feature = (name, enabled)
            return "restart-required"

    backend = Backend()
    controller = BridgeController(
        backend=backend, setup=object(), subscribe=False, autostart=False,
        doctor=lambda: DoctorReport(True, True, "WARNING something"),
    )
    monkeypatch.setattr(
        controller, "_run",
        lambda operation, on_done=None, *_args, **_kwargs: on_done(operation()),
    )
    controller.loadFeatures()
    assert controller.features["available"] is True
    assert controller.features["items"]["mark_read_on_dismiss"]["restart_required"] is True
    controller.setFeature("mark_read_on_dismiss", False)
    assert backend.feature == ("mark_read_on_dismiss", False)
    assert "Restart" in controller.features["notice"]
    assert controller.features["restart_pending"] is True
    assert controller.features["switches"]["mark_read_on_dismiss"]["editable"] is True
    controller.runDoctor()
    _wait(lambda: controller.doctor["ran"])
    assert controller.doctor["warnings"] is True and controller.doctor["text"] == "WARNING something"
    assert controller.doctor["level"] == "warning"


def test_older_daemon_without_features_falls_back_to_hints(app, monkeypatch) -> None:
    from blueferry.client import BackendError
    from blueferry.qt.controller import BridgeController
    from tests.test_qt_controller import _Backend

    class Backend(_Backend):
        def features(self):
            raise BackendError("No such method 'GetFeatures'")

    controller = BridgeController(backend=Backend(), setup=object(), subscribe=False,
                                  autostart=False)
    monkeypatch.setattr(
        controller, "_run",
        lambda operation, on_done=None, on_failed=None, **_kwargs: (
            on_failed(str(_capture(operation)))
        ),
    )
    controller.loadFeatures()
    assert controller.features["loaded"] is True and controller.features["available"] is False
    assert controller.features["switches"]["mark_read_on_dismiss"]["usable"] is False


def _capture(operation):
    try:
        operation()
    except Exception as error:  # the BackendError the worker would report
        return error
    raise AssertionError("expected a failure")


def test_a_feature_switch_keeps_its_choice_while_it_is_saved(app, monkeypatch) -> None:
    from blueferry.qt.controller import BridgeController
    from tests.test_qt_controller import _Backend

    class Backend(_Backend):
        stored = False

        def features(self):
            return {"mark_read_on_dismiss": {
                "value": self.stored, "running": False, "source": "settings",
                "restart_required": self.stored}}

        def set_feature(self, name, enabled):
            self.stored = enabled
            return "restart-required"

    backend = Backend()
    controller = BridgeController(backend=backend, setup=object(), subscribe=False,
                                  autostart=False)
    held = []
    monkeypatch.setattr(controller, "_run", lambda operation, on_done=None, on_failed=None,
                        **_kwargs: held.append((operation, on_done, on_failed)))
    controller.loadFeatures()
    operation, done, _failed = held.pop()
    done(operation())
    switch = lambda: controller.features["switches"]["mark_read_on_dismiss"]  # noqa: E731
    assert switch()["active"] is False
    controller.setFeature("mark_read_on_dismiss", True)
    # Before SetFeature answers, and after it until GetFeatures does.
    assert switch()["active"] is True
    operation, done, _failed = held.pop()
    done(operation())
    assert switch()["active"] is True and held  # the reload is queued
    held.clear()
    # A failed save puts the previous state back and reports the error.
    controller.setFeature("mark_read_on_dismiss", False)
    assert switch()["active"] is False
    held.pop()[2]("Access denied")
    assert switch()["active"] is True
    assert controller.errorText == "Access denied"


@pytest.mark.parametrize("failed_read", [False, True])
def test_stale_feature_reads_cannot_undo_a_new_choice(app, monkeypatch, failed_read):
    from blueferry import features_view
    from blueferry.qt.controller import BridgeController
    from tests.test_qt_controller import _Backend

    controller = BridgeController(backend=_Backend(), setup=object(), subscribe=False,
                                  autostart=False)
    items = {"mark_read_on_dismiss": {"value": False, "running": False}}
    controller._features = features_view.loaded(features_view.initial(), items)
    held = []
    monkeypatch.setattr(controller, "_run", lambda operation, on_done=None, on_failed=None,
                        **_kwargs: held.append((on_done, on_failed)))
    controller.loadFeatures()
    read_done, read_failed = held.pop()
    controller.setFeature("mark_read_on_dismiss", True)
    saved, _failed = held.pop()
    if failed_read:
        read_failed("old daemon disappeared")
    else:
        read_done(items)
    saved("restart-required")
    assert controller.features["switches"]["mark_read_on_dismiss"]["active"]
    assert controller.features["restart_pending"]


def test_failed_feature_save_preserves_another_pending_choice(app, monkeypatch):
    from blueferry import features_view
    from blueferry.qt.controller import BridgeController
    from tests.test_qt_controller import _Backend

    controller = BridgeController(backend=_Backend(), setup=object(), subscribe=False,
                                  autostart=False)
    controller._features = features_view.loaded(features_view.initial(), {
        name: {"value": False, "running": False}
        for name in ("mark_read_on_dismiss", "notification_history")
    })
    held = []
    monkeypatch.setattr(controller, "_run", lambda operation, on_done=None, on_failed=None,
                        **_kwargs: held.append((on_done, on_failed)))
    controller.setFeature("notification_history", True)
    _saved, failed = held.pop()
    controller.setFeature("mark_read_on_dismiss", True)
    saved, _failed = held.pop()
    failed("access denied")
    assert not held  # Wait until all saves finish before reloading.
    saved("restart-required")
    assert not controller.features["switches"]["notification_history"]["active"]
    assert controller.features["switches"]["mark_read_on_dismiss"]["active"]
    assert controller.features["restart_pending"]
