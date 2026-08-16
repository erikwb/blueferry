"""Behavioral checks for extracted QML presentation components."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

# These checks instantiate QML components without displaying them.  Do not
# inherit a developer session's Wayland/X11 backend inside makepkg's private
# D-Bus test session; Qt can otherwise wait indefinitely for desktop services.
os.environ["QT_QPA_PLATFORM"] = "offscreen"

pytest.importorskip("PySide6")

from PySide6.QtCore import QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlComponent, QQmlEngine

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def qml_engine():
    application = QGuiApplication.instance() or QGuiApplication([])
    engine = QQmlEngine()
    yield engine
    engine.deleteLater()
    application.processEvents()


def _component(engine: QQmlEngine, relative_path: str) -> QQmlComponent:
    component = QQmlComponent(
        engine, QUrl.fromLocalFile(str((ROOT / relative_path).resolve()))
    )
    assert not component.isError(), "\n".join(
        error.toString() for error in component.errors()
    )
    return component


def test_quickshell_onboarding_state_derives_ready_stage(qml_engine) -> None:
    component = _component(qml_engine, "data/quickshell/OnboardingState.qml")
    presenter = component.createWithInitialProperties({
        "notificationsSupported": True,
        "bluezActive": True,
        "configured": True,
        "backendStatus": {
            "daemon": True,
            "map": True,
            "pbap": True,
            "verified_iphone_setup": [
                "message-notifications", "contacts", "notification-access",
            ],
        },
    })

    assert presenter is not None
    assert presenter.property("stage") == "ready"
    presenter.deleteLater()


def test_quickshell_unverified_controller_still_reaches_device_selection(
    qml_engine,
) -> None:
    component = _component(qml_engine, "data/quickshell/OnboardingState.qml")
    presenter = component.createWithInitialProperties({
        "notificationsSupported": False,
        "bluezActive": False,
        "configured": False,
        "backendStatus": {},
    })

    assert presenter is not None
    assert presenter.property("stage") == "select-device"
    presenter.deleteLater()


def test_qt_onboarding_summary_renders_stage_from_properties(qml_engine) -> None:
    component = _component(
        qml_engine, "src/blueferry/qt/qml/OnboardingSummary.qml"
    )
    summary = component.createWithInitialProperties({
        "stage": "ready",
        "compatibility": {"notifications_supported": True},
        "status": {"verified_iphone_setup": []},
    })

    assert summary is not None
    assert "BlueFerry Is Connected" in summary.property("text")
    summary.deleteLater()


def test_qt_onboarding_summary_explains_locked_contact_sync(qml_engine) -> None:
    component = _component(
        qml_engine, "src/blueferry/qt/qml/OnboardingSummary.qml"
    )
    summary = component.createWithInitialProperties({
        "stage": "iphone-settings",
        "compatibility": {"notifications_supported": False},
        "status": {"verified_iphone_setup": ["message-notifications"]},
    })

    assert summary is not None
    assert summary.setProperty("storagePolicy", "encrypted")
    assert summary.setProperty("storageState", "locked")
    assert summary.property("storagePolicy") == "encrypted"
    assert summary.property("storageState") == "locked"
    assert "Unlock Local Data, then sync contacts again" in summary.property("text")
    assert "Enable Sync Contacts" not in summary.property("text")
    summary.deleteLater()
