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

from PySide6.QtCore import Property, QObject, QUrl, Slot
from PySide6.QtGui import QColor, QGuiApplication
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


class _BubbleTheme(QObject):
    @Property(QColor, constant=True)
    def windowText(self) -> QColor:
        return QColor("#f4f4f4")

    @Property(QColor, constant=True)
    def selectedSurface(self) -> QColor:
        return QColor("#334455")

    @Property(QColor, constant=True)
    def raisedSurface(self) -> QColor:
        return QColor("#222222")

    @Property(QColor, constant=True)
    def divider(self) -> QColor:
        return QColor("#555555")

    @Property(QColor, constant=True)
    def muted(self) -> QColor:
        return QColor("#999999")

    @Property(str, constant=True)
    def fontFamily(self) -> str:
        return "Sans Serif"

    @Property(int, constant=True)
    def captionSize(self) -> int:
        return 10

    @Property(int, constant=True)
    def baseFontSize(self) -> int:
        return 12

    @Property(int, constant=True)
    def controlRadius(self) -> int:
        return 8

    @Slot(float, result=int)
    def scaled(self, value: float) -> int:
        return max(1, round(value))


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


def test_quickshell_long_message_is_truncated_to_the_timeline_height(
    qml_engine,
) -> None:
    component = _component(
        qml_engine,
        "data/quickshell/QuickshellMessageBubble.qml",
    )
    theme = _BubbleTheme()
    bubble = component.createWithInitialProperties({
        "message": {
            "outgoing": False,
            "sender": "A friend",
            "body": " ".join(["long message"] * 300),
            "display_timestamp": "10:42 PM",
        },
        "availableWidth": 480.0,
        "availableHeight": 220.0,
        "showSender": True,
        "ferryTheme": theme,
    })
    QGuiApplication.processEvents()

    assert bubble is not None
    assert bubble.property("height") > 0
    assert bubble.property("height") <= 217
    assert bubble.property("bodyTruncated") is True
    assert bubble.property("height") == min(
        bubble.property("naturalHeight"), bubble.property("maximumHeight")
    )
    assert bubble.property("canRenderBody") is True
    assert bubble.property("width") <= 480 * 0.76
    assert bubble.property("showSenderChrome") is True
    assert bubble.property("showTimestampChrome") is True

    assert bubble.setProperty("availableHeight", 0)
    QGuiApplication.processEvents()
    assert bubble.property("height") == 0
    assert bubble.property("canRenderBody") is False
    assert bubble.property("bodyTruncated") is False

    minimum_viewport_height = bubble.property("minimumBodyHeight") + 3
    assert bubble.setProperty("availableHeight", minimum_viewport_height)
    QGuiApplication.processEvents()
    assert bubble.property("height") > 0
    assert bubble.property("naturalHeight") <= bubble.property("maximumHeight")
    assert bubble.property("showSenderChrome") is False
    assert bubble.property("showTimestampChrome") is False

    assert bubble.setProperty("availableHeight", 220.0)
    QGuiApplication.processEvents()
    assert bubble.property("height") > 0
    assert bubble.property("bodyTruncated") is True
    assert bubble.property("showSenderChrome") is True
    assert bubble.property("showTimestampChrome") is True
    bubble.deleteLater()


def test_quickshell_fallback_glyphs_cannot_overflow_timeline(qml_engine) -> None:
    component = _component(
        qml_engine,
        "data/quickshell/QuickshellMessageBubble.qml",
    )
    theme = _BubbleTheme()
    bubble = component.createWithInitialProperties({
        "message": {
            "outgoing": False,
            "sender": "A friend",
            "body": "emoji 🚀 漢字 " * 200,
            "display_timestamp": "10:42 PM",
        },
        "availableWidth": 480.0,
        "availableHeight": 220.0,
        "showSender": True,
        "ferryTheme": theme,
    })
    QGuiApplication.processEvents()

    assert bubble is not None
    assert bubble.property("height") <= bubble.property("maximumHeight")
    assert bubble.property("height") == min(
        bubble.property("naturalHeight"), bubble.property("maximumHeight")
    )
    assert bubble.property("bodyTruncated") is True
    assert bubble.property("clip") is True
    bubble.deleteLater()


def test_quickshell_short_message_uses_its_natural_height(qml_engine) -> None:
    component = _component(
        qml_engine,
        "data/quickshell/QuickshellMessageBubble.qml",
    )
    theme = _BubbleTheme()
    bubble = component.createWithInitialProperties({
        "message": {
            "outgoing": True,
            "sender": "",
            "body": "A short message",
            "display_timestamp": "10:43 PM",
        },
        "availableWidth": 480.0,
        "availableHeight": 220.0,
        "showSender": False,
        "ferryTheme": theme,
    })
    QGuiApplication.processEvents()

    assert bubble is not None
    assert 0 < bubble.property("height") < 217
    assert bubble.property("height") == bubble.property("naturalHeight")
    assert bubble.property("bodyTruncated") is False
    bubble.deleteLater()


def test_quickshell_thread_preview_stays_inside_one_line(qml_engine) -> None:
    component = _component(
        qml_engine,
        "data/quickshell/QuickshellThreadPreview.qml",
    )
    theme = _BubbleTheme()
    preview = component.createWithInitialProperties({
        "thread": {
            "messages": [{
                "outgoing": True,
                "body": (
                    "A long opening line that cannot fit in the sidebar\n"
                    "and a second line that must not escape the thread row"
                ),
            }],
        },
        "ferryTheme": theme,
        "width": 120.0,
    })
    QGuiApplication.processEvents()

    assert preview is not None
    assert preview.property("lineCount") == 1
    assert preview.property("truncated") is True
    assert preview.property("height") == preview.property("implicitHeight")
    preview.deleteLater()


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
