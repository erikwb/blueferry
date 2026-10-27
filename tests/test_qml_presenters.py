"""Behavioral checks for extracted QML presentation components."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest

# These checks instantiate QML components without displaying them.  Do not
# inherit a developer session's Wayland/X11 backend inside makepkg's private
# D-Bus test session; Qt can otherwise wait indefinitely for desktop services.
os.environ["QT_QPA_PLATFORM"] = "offscreen"

pytest.importorskip("PySide6")

from PySide6.QtCore import (
    Q_ARG,
    Property,
    QBuffer,
    QByteArray,
    QEvent,
    QIODevice,
    QMetaObject,
    QObject,
    QPointF,
    Qt,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import QColor, QDesktopServices, QGuiApplication, QImage
from PySide6.QtQml import QQmlComponent, QQmlEngine, qmlEngine
from PySide6.QtQuick import QQuickWindow
from PySide6.QtTest import QTest

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
    def outgoingBubble(self) -> QColor:
        return QColor("#245baf")

    @Property(QColor, constant=True)
    def outgoingText(self) -> QColor:
        return QColor("#ffffff")

    @Property(QColor, constant=True)
    def outgoingMuted(self) -> QColor:
        return QColor("#d2e2fa")

    @Property(QColor, constant=True)
    def control(self) -> QColor:
        return QColor("#222222")

    @Property(QColor, constant=True)
    def raisedSurface(self) -> QColor:
        return QColor("#222222")

    @Property(QColor, constant=True)
    def divider(self) -> QColor:
        return QColor("#555555")

    @Property(QColor, constant=True)
    def muted(self) -> QColor:
        return QColor("#999999")

    @Property(QColor, constant=True)
    def accent(self) -> QColor:
        return QColor("#4488cc")

    @Property(QColor, constant=True)
    def highlightedText(self) -> QColor:
        return QColor("#ffffff")

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


def test_quickshell_reports_a_suspect_le_bond(qml_engine) -> None:
    component = _component(qml_engine, "data/quickshell/OnboardingState.qml")
    presenter = component.createWithInitialProperties({
        "notificationsSupported": True,
        "bluezActive": True,
        "configured": True,
        "backendStatus": {"daemon": True, "le_bond_suspect": True},
    })
    assert presenter is not None
    qml_engine.globalObject().setProperty(
        "testOnboarding", qml_engine.newQObject(presenter)
    )
    assert _evaluate(qml_engine, "testOnboarding.leBondSuspect()") is True
    presenter.setProperty("backendStatus", {"daemon": True, "le_bond_suspect": "yes"})
    assert _evaluate(qml_engine, "testOnboarding.leBondSuspect()") is False
    presenter.setProperty("backendStatus", {"daemon": True})
    assert _evaluate(qml_engine, "testOnboarding.leBondSuspect()") is False
    shell = (ROOT / "data/quickshell/shell.qml").read_text()
    assert "onboarding.leBondSuspect()" in shell
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


def test_qt_incompatible_adapter_explains_missing_capabilities(qml_engine):
    from blueferry.models import BackendStatus
    from blueferry.onboarding import derive_stage

    compatibility = {
        "pairing_ready": False,
        "issue": "Incompatible Bluetooth adapter: missing Bluetooth LE and LE advertising",
    }
    stage = derive_stage(
        setup_loaded=True, configured=False,
        compatibility=compatibility, status=BackendStatus(),
    )
    component = _component(qml_engine, "src/blueferry/qt/qml/OnboardingSummary.qml")
    summary = component.createWithInitialProperties({
        "stage": str(stage), "compatibility": compatibility, "status": {},
    })
    assert summary is not None
    assert "Incompatible Bluetooth Adapter" in summary.property("text")
    assert compatibility["issue"] in summary.property("text")
    summary.deleteLater()


def test_quickshell_long_message_is_truncated_to_the_timeline_height(
    qml_engine,
) -> None:
    from blueferry.message_links import linkify_message

    component = _component(
        qml_engine,
        "data/quickshell/QuickshellMessageBubble.qml",
    )
    theme = _BubbleTheme()
    text = " ".join(["long message https://example.com/"] * 300)
    bubble = component.createWithInitialProperties({
        "message": {
            "outgoing": False,
            "sender": "A friend",
            "body": text,
            "body_markup": linkify_message(text),
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

    body = bubble.findChild(QObject, "messageBody")
    content = bubble.findChild(QObject, "bubbleContent")
    timestamp = bubble.findChild(QObject, "messageTimestamp")
    overflow = bubble.findChild(QObject, "messageOverflowIndicator")
    overflow_glyph = bubble.findChild(QObject, "messageOverflowGlyph")
    assert body is not None
    assert content is not None
    assert timestamp is not None
    assert overflow is not None
    assert overflow_glyph is not None
    assert body.property("height") <= bubble.property("maximumBodyHeight")
    assert overflow_glyph.property("text") == "…"
    assert overflow.property("y") + overflow.property("height") <= body.property(
        "height"
    )
    timestamp_bottom = (
        content.property("y")
        + timestamp.property("y")
        + timestamp.property("height")
        + bubble.property("bubblePadding")
    )
    assert timestamp_bottom <= bubble.property("height")

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


def test_quickshell_message_body_is_selectable_and_copyable(qml_engine) -> None:
    component = _component(
        qml_engine,
        "data/quickshell/QuickshellMessageBubble.qml",
    )
    theme = _BubbleTheme()
    message_text = "Verification code: 123456 " + "more details " * 200
    bubble = component.createWithInitialProperties({
        "message": {
            "outgoing": False,
            "sender": "A friend",
            "body": message_text,
            "display_timestamp": "10:44 PM",
        },
        "availableWidth": 480.0,
        "availableHeight": 220.0,
        "showSender": False,
        "ferryTheme": theme,
    })
    QGuiApplication.processEvents()

    assert bubble is not None
    body = bubble.findChild(QObject, "messageBody")
    assert body is not None
    assert body.property("readOnly") is True
    assert body.property("selectByMouse") is True
    assert body.property("activeFocusOnTab") is False
    assert bubble.property("bodyTruncated") is True
    assert QMetaObject.invokeMethod(body, "selectAll") is True
    assert body.property("selectedText") == message_text

    clipboard = QGuiApplication.clipboard()
    clipboard.clear()
    assert QMetaObject.invokeMethod(body, "copy") is True
    assert clipboard.text() == message_text
    clipboard.clear()
    bubble.deleteLater()


@pytest.mark.parametrize("client", ["qt", "quickshell"])
@pytest.mark.parametrize("outgoing", [False, True])
@pytest.mark.parametrize("line_ending", ["\n", "\r\n", "\r"])
def test_message_links_open_on_click_and_preserve_copy(
    qml_engine, client, outgoing, line_ending,
):
    from blueferry.models import ThreadMessage

    class UrlHandler(QObject):
        def __init__(self):
            super().__init__()
            self.urls = []

        @Slot(QUrl)
        def open_url(self, url):
            self.urls.append(url.toString())

    theme = _BubbleTheme()
    text = (
        '\n  <b>literal</b> 🚀\n“https://example.com/a_(b)?x=1&y=2”—see above\n\n'
        '(www.example.org/page)—see above\n\t end  \n'
    )
    original = text.replace("\n", line_ending)
    properties = {
        "message": ThreadMessage.from_dict({"body": original, "outgoing": outgoing}).to_dict(),
        "availableWidth": 480.0, "showSender": False,
    }
    if client == "quickshell":
        path = "data/quickshell/QuickshellMessageBubble.qml"
        properties.update(availableHeight=600.0, ferryTheme=theme)
    else:
        path = "src/blueferry/qt/qml/MessageBubble.qml"
    component = _component(qml_engine, path)
    bubble = component.createWithInitialProperties(properties)
    assert bubble is not None
    window = QQuickWindow()
    window.resize(480, 600)
    bubble.setParentItem(window.contentItem())
    if client == "qt":
        bubble.setHeight(bubble.implicitHeight())
    body = bubble.findChild(QObject, "messageBody")
    assert body is not None
    qml_engine.globalObject().setProperty("linkBody", qml_engine.newQObject(body))
    handler = UrlHandler()
    QDesktopServices.setUrlHandler("https", handler, "open_url")

    def point_at(position):
        # QML text positions count UTF-16 code units, including emoji.
        position = len(text[:position].encode("utf-16-le")) // 2
        return _evaluate(qml_engine, f'''(() => {{
            const rect = linkBody.positionToRectangle({position});
            return linkBody.mapToItem(null, rect.x + 1, rect.y + rect.height / 2);
        }})()''').toPoint()

    try:
        window.show()
        QTest.qWait(30)
        assert not handler.urls  # Rendering alone must never open the browser.
        for label, url in [
            ("https://example.com/a_(b)?x=1&y=2", "https://example.com/a_(b)?x=1&y=2"),
            ("www.example.org/page", "https://www.example.org/page"),
        ]:
            QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier,
                             point_at(text.index(label) + 3))
            QGuiApplication.processEvents()
            assert handler.urls[-1:] == [url]
        assert len(handler.urls) == 2
        # Selecting part of a URL must not also launch it on mouse release.
        start = text.index("https://") + 2
        QTest.mousePress(window, Qt.LeftButton, Qt.NoModifier, point_at(start))
        QTest.mouseMove(window, point_at(start + 10), delay=30)
        QTest.mouseRelease(window, Qt.LeftButton, Qt.NoModifier, point_at(start + 10))
        assert body.property("selectedText")
        assert len(handler.urls) == 2
        QMetaObject.invokeMethod(body, "selectAll")
        clipboard = QGuiApplication.clipboard()
        clipboard.clear()
        QMetaObject.invokeMethod(body, "copy")
        assert clipboard.text() == text
        clipboard.clear()
        assert len(handler.urls) == 2
        assert _evaluate(qml_engine, "linkBody.wrapMode") != 0
        assert body.property("lineCount") >= text.count("\n")
        assert body.property("contentWidth") <= body.property("width")
        rich_line_count = body.property("lineCount")
        # Compare with the old plain-text renderer, including CRLF handling.
        plain_message = dict(properties["message"])
        del plain_message["body_markup"]
        assert plain_message["body"] == original
        plain_bubble = component.createWithInitialProperties({
            **properties, "message": plain_message,
        })
        assert plain_bubble is not None
        try:
            QGuiApplication.processEvents()
            plain_body = plain_bubble.findChild(QObject, "messageBody")
            assert plain_body.property("lineCount") == rich_line_count
        finally:
            plain_bubble.deleteLater()
    finally:
        QDesktopServices.unsetUrlHandler("https")
        qml_engine.globalObject().deleteProperty("linkBody")
        window.close()
        bubble.deleteLater()
        window.deleteLater()
        QGuiApplication.processEvents()


@pytest.mark.parametrize("client", ["qt", "quickshell"])
def test_message_link_activation_rechecks_the_scheme(qml_engine, client):
    class UrlHandler(QObject):
        def __init__(self):
            super().__init__()
            self.urls = []

        @Slot(QUrl)
        def open_url(self, url):
            self.urls.append(url.toString())

    theme = _BubbleTheme()
    properties = {
        "message": {"body": "Message text", "outgoing": False},
        "availableWidth": 480.0, "showSender": False,
    }
    if client == "quickshell":
        path = "data/quickshell/QuickshellMessageBubble.qml"
        properties.update(availableHeight=600.0, ferryTheme=theme)
    else:
        path = "src/blueferry/qt/qml/MessageBubble.qml"
    component = _component(qml_engine, path)
    bubble = component.createWithInitialProperties(properties)
    assert bubble is not None
    body = bubble.findChild(QObject, "messageBody")
    assert body is not None
    handler = UrlHandler()
    schemes = ("http", "https", "file", "javascript", "data", "mailto", "custom")
    for scheme in schemes:
        QDesktopServices.setUrlHandler(scheme, handler, "open_url")
    try:
        # Inject at the activation signal, bypassing the formatter to exercise
        # the final guard. All desktop URL handlers are intercepted.
        for rejected in (
            "file:///tmp/blueferry-link-test", "javascript:alert(1)", "data:text/plain,hello",
            "mailto:friend@example.com", "custom:action", "//example.com", "https:example.com",
            " https://example.com", "relative/path",
        ):
            assert QMetaObject.invokeMethod(body, "linkActivated", Q_ARG(str, rejected))
        assert handler.urls == []
        for accepted in ("http://example.com", "HTTPS://example.com/path"):
            assert QMetaObject.invokeMethod(body, "linkActivated", Q_ARG(str, accepted))
        assert handler.urls == ["http://example.com", "https://example.com/path"]
    finally:
        for scheme in schemes:
            QDesktopServices.unsetUrlHandler(scheme)
        bubble.deleteLater()
        QGuiApplication.processEvents()


def test_quickshell_phone_status_shows_only_known_battery_and_signal(qml_engine) -> None:
    component = _component(qml_engine, "data/quickshell/QuickshellPhoneStatus.qml")
    theme = _BubbleTheme()

    hidden = component.createWithInitialProperties({
        "ferryTheme": theme,
        "status": {"calls_enabled": True, "phone_battery_level": None,
                   "phone_network_name": "Sunrise"},
    })
    assert hidden is not None
    assert hidden.property("visible") is False
    assert hidden.property("text") == ""

    shown = component.createWithInitialProperties({
        "ferryTheme": theme,
        "status": {"phone_battery_level": 60, "phone_signal_strength": 80,
                   "phone_network_name": "<b>Sunrise</b>"},
    })
    assert shown.property("visible") is True
    # The operator name is deliberately not part of the compact header.
    assert shown.property("text") == "BATTERY 60 % · SIGNAL 80 %"
    stepped = component.createWithInitialProperties({
        "ferryTheme": theme,
        "status": {"phone_battery_level": 40, "phone_battery_source": "hfp"},
    })
    assert stepped.property("text") == "BATTERY ~40 %"
    stepped.deleteLater()

    malformed = component.createWithInitialProperties({
        "ferryTheme": theme,
        "status": {"phone_battery_level": "60", "phone_signal_strength": 400},
    })
    assert malformed.property("visible") is False
    for item in (hidden, shown, malformed):
        item.deleteLater()


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


def test_qt_thread_preview_stays_inside_one_line(qml_engine, settings_window) -> None:
    window, bridge = settings_window
    bridge.setProperty("threads", [{
        "key": "one", "name": "Friend", "is_group": False, "starred": False,
        "recipients": ["+15550001111"],
        "messages": [{
            "outgoing": False,
            "body": (
                "A long opening line that cannot fit in the sidebar at all, ever\n\n"
                "and a second paragraph that must not escape the thread row"
            ),
        }],
    }])
    QGuiApplication.processEvents()

    # Delegates are not QObject children of the window; look them up in QML.
    qml_engine.globalObject().setProperty(
        "threadList", qml_engine.newQObject(_settings_object(window, "threadList"))
    )
    preview = _evaluate(qml_engine, """(function() {
        function find(item) {
            // The phone-link layout shows the preview as the ListRow subtitle.
            if (item.objectName === "threadPreview" || item.objectName.endsWith("Subtitle")) return item;
            for (const child of item.children) {
                const found = find(child);
                if (found) return found;
            }
            return null;
        }
        const label = find(threadList.itemAtIndex(0));
        return [label.text, label.lineCount, label.truncated];
    })()""")

    assert "\n" not in preview[0]
    assert preview[1:] == [1, True]


def test_qt_onboarding_summary_treats_realtek_as_expected_success(qml_engine) -> None:
    component = _component(
        qml_engine, "src/blueferry/qt/qml/OnboardingSummary.qml"
    )
    summary = component.createWithInitialProperties({
        "stage": "ready-without-ancs",
        "compatibility": {
            "notifications_supported": False,
            "ancs_limited_controller": True,
            "controller_vendor": "Realtek",
        },
        "status": {"verified_iphone_setup": []},
    })

    assert summary is not None
    text = summary.property("text")
    assert "Messages Are Connected" in text
    assert "This Realtek adapter does not support iPhone system notifications" in text
    assert "System notifications are unavailable" not in text
    summary.deleteLater()


def test_qt_onboarding_summary_uses_the_supplied_bluetooth_restart_command(
    qml_engine,
) -> None:
    component = _component(
        qml_engine, "src/blueferry/qt/qml/OnboardingSummary.qml"
    )
    status = {"verified_iphone_setup": []}
    compatibility = {"notifications_supported": False}
    default = component.createWithInitialProperties({
        "stage": "ready-without-ancs",
        "compatibility": compatibility,
        "status": status,
    })
    openrc = component.createWithInitialProperties({
        "stage": "ready-without-ancs",
        "compatibility": compatibility,
        "status": status,
        "bluetoothRestartCommand": "sudo rc-service bluetooth restart",
    })

    assert default is not None and openrc is not None
    qml_engine.globalObject().setProperty("defaultSummary", qml_engine.newQObject(default))
    qml_engine.globalObject().setProperty("openrcSummary", qml_engine.newQObject(openrc))
    default_hint = _evaluate(qml_engine, "defaultSummary.ancsUnavailableHint()")
    openrc_hint = _evaluate(qml_engine, "openrcSummary.ancsUnavailableHint()")
    assert "sudo systemctl restart bluetooth.service" in default_hint
    assert "sudo rc-service bluetooth restart" in openrc_hint
    assert "systemctl" not in openrc_hint
    default.deleteLater()
    openrc.deleteLater()


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


def _qml_functions(source: str, names: tuple[str, ...]) -> str:
    functions = []
    for name in names:
        start = source.index("function " + name + "(")
        opening = source.index("{", start)
        depth = 1
        end = opening + 1
        while depth:
            depth += (source[end] == "{") - (source[end] == "}")
            end += 1
        functions.append(source[start:end])
    return "\n".join(functions)


@pytest.fixture
def settings_window(qml_engine):
    """Load an offscreen window with a recorder that has no backend access."""
    component = QQmlComponent(qml_engine)
    component.setData(b'''
        import QtQuick
        QtObject {
            property var calls: []
            property var phoneCalls: []
            property string callsState: "disabled"
            property var status: ({})
            property var threads: []
            property var devices: []
            property var contactResults: []
            property var nowPlaying: ({})
            property var featureHints: ({})
            property var phoneAudio: ({})
            property string phoneName: "Test iPhone"
            property var notifications: []
            property var notificationsInfo: ({})
            property var callHistoryRows: ({})
            property var photos: ({})
            property var pluginSettings: ({})
            property var pluginSurfaces: ({})
            property var features: ({})
            property var doctor: ({})
            property var reconnect: ({})
            property var compatibility: ({})
            property var onboardingCompatibility: compatibility
            property bool compatibilityLoaded: false
            property bool bluetoothActive: false
            property bool busy: false
            property bool configured: false
            property bool targetSaved: false
            property bool setupLoaded: false
            property string configuredMac: ""
            property string onboardingStage: "loading"
            property string errorText: ""
            property string pairingIssueReport: ""
            property string version: "test"
            property bool callHistoryEnabled: false
            property var callHistory: []
            property string callHistoryError: ""
            property string bluetoothRestartCommand: "sudo rc-service bluetooth restart"
            signal pairingConfirmationRequested(string passkey)
            signal messageOpenRequested(string handle)
            signal messageSendSucceeded(string recipient, string body)
            signal threadSendSucceeded(string key, string body)
            signal groupConfirmationRequested(string key, string body, string recipients)
            signal dialLinkRequested(string number, string problem)
            property var contactNames: ({})
            function contactNameForNumber(number) { return contactNames[number] || ""; }
            function record(method, args) { calls = calls.concat([{method: method, args: args}]); }
            function refresh() { record("refresh", []); }
            function findContacts(query) { record("findContacts", [query]); }
            function selectAdapter(adapter) { record("selectAdapter", [adapter]); }
            function completePairing(mac, compatibility, explicit) {
                record("completePairing", [mac, compatibility, explicit]);
            }
            function replaceAndPair(previousMac, mac, compatibility, explicit) {
                record("replaceAndPair", [previousMac, mac, compatibility, explicit]);
            }
            function answerPairingConfirmation(approved) { record("answerPairingConfirmation", [approved]); }
            function setStoragePolicy(policy) { record("setStoragePolicy", [policy]); }
            function setProximityLock(enabled, grace) { record("setProximityLock", [enabled, grace]); }
            function setCallHistory(enabled, popups) { record("setCallHistory", [enabled, popups]); }
            property var notificationOpenMap: []
            function loadNotificationOpenMap() { record("loadNotificationOpenMap", []); }
            function setNotificationOpenTarget(bundle, target) { record("setNotificationOpenTarget", [bundle, target]); }
            function removeNotificationOpenTarget(bundle) { record("removeNotificationOpenTarget", [bundle]); }
            function setMediaControl(enabled) { record("setMediaControl", [enabled]); }
            function setAncsNotificationActions(enabled) { record("setAncsNotificationActions", [enabled]); }
            function setMirrorNotificationRemovals(enabled) { record("setMirrorNotificationRemovals", [enabled]); }
            function setCallsEnabled(enabled) { record("setCallsEnabled", [enabled]); }
            function setPhoneBatteryWarning(enabled) { record("setPhoneBatteryWarning", [enabled]); }
            function setAutoProfileReset(enabled) { record("setAutoProfileReset", [enabled]); }
            function setMprisPlayer(enabled) { record("setMprisPlayer", [enabled]); }
            function setPhoneAudioRoute(route) { record("setPhoneAudioRoute", [route]); }
            function swapCalls() { record("swapCalls", []); }
            function watchNotifications(watched) { record("watchNotifications", [watched]); }
            function refreshNotifications() { record("refreshNotifications", []); }
            function setThreadStarred(key, starred) { record("setThreadStarred", [key, starred]); }
            function markThreadRead(key) { record("markThreadRead", [key]); }
            function watchPhotos(watched) { record("watchPhotos", [watched]); }
            function refreshPhotos() { record("refreshPhotos", []); }
            function openPhoto(photoId) { record("openPhoto", [photoId]); }
            function loadPlugins() { record("loadPlugins", []); }
            function loadPluginStore(refresh) { record("loadPluginStore", [refresh]); }
            function setPluginEnabled(id, enabled) { record("setPluginEnabled", [id, enabled]); }
            function preparePluginInstall(url, ref) { record("preparePluginInstall", [url, ref]); }
            function installStorePlugin(id) { record("installStorePlugin", [id]); }
            function preparePluginUpdate(id) { record("preparePluginUpdate", [id]); }
            function confirmPluginInstall() { record("confirmPluginInstall", []); }
            function cancelPluginInstall() { record("cancelPluginInstall", []); }
            function removePlugin(id) { record("removePlugin", [id]); }
            function loadPluginConfig(id) { record("loadPluginConfig", [id]); }
            function savePluginConfig(id, values) { record("savePluginConfig", [id, values]); }
            function closePluginConfig() { record("closePluginConfig", []); }
            property var checkErrors: ({})
            function checkPluginConfig(id, values) {
                const fields = (pluginSettings.config || {}).fields || []
                const merged = {}
                fields.forEach(f => merged[f.key] = (f.key in values) ? values[f.key] : f.value)
                const visible = fields.filter(f => !f.showIfKey
                    || String(merged[f.showIfKey]) === f.showIfValue).map(f => f.key)
                return {errors: checkErrors, visible: visible,
                        valid: Object.keys(checkErrors).length === 0}
            }
            function testPluginConfig(id, values) { record("testPluginConfig", [id, values]); }
            function signInPlugin(id, values) { record("signInPlugin", [id, values]); }
            function cancelPluginSignIn() { record("cancelPluginSignIn", []); }
            function openPluginHelp(id, key) { record("openPluginHelp", [id, key]); }
            function openPluginLog(id) { record("openPluginLog", [id]); }
            function setPluginIndexes(urls) { record("setPluginIndexes", [urls]); }
            function clearPluginMessage() { record("clearPluginMessage", []); }
            property int featureLoads: 0
            function loadFeatures() { featureLoads += 1; }
            function setFeature(name, enabled) { record("setFeature", [name, enabled]); }
            function runDoctor() { record("runDoctor", []); }
            function restartBackend() { record("restartBackend", []); }
            function syncContacts() { record("syncContacts", []); }
            function setContactPhotos(enabled) { record("setContactPhotos", [enabled]); }
            function setOtpAutocopy(enabled) { record("setOtpAutocopy", [enabled]); }
            function forgetDevice(mac) { record("forgetDevice", [mac]); }
            function activateBluetooth() { record("activateBluetooth", []); }
            function filePairingIssue() { record("filePairingIssue", []); }
            function watchCallHistory(watched) { record("watchCallHistory", [watched]); }
            function loadCallHistory() { record("loadCallHistory", []); }
            function syncCallHistory() { record("syncCallHistory", []); }
            function sendMediaCommand(command) { record("sendMediaCommand", [command]); }
            function refreshCalls() { record("refreshCalls", []); }
            function dialCall(number) { record("dialCall", [number]); }
            function answerCall(callId) { record("answerCall", [callId]); }
            function hangupCall(callId) { record("hangupCall", [callId]); }
            function hangupAllCalls() { record("hangupAllCalls", []); }
            function reconnectPhone() { record("reconnectPhone", []); }
        }
    ''', QUrl())
    assert not component.isError(), [error.toString() for error in component.errors()]
    bridge = component.create()
    assert bridge is not None
    warnings = []

    def collect_warnings(errors):
        warnings.extend(error.toString() for error in errors)

    qml_engine.warnings.connect(collect_warnings)
    window_component = _component(qml_engine, "src/blueferry/qt/qml/Main.qml")
    window = window_component.createWithInitialProperties({"bridge": bridge})
    assert window is not None, [error.toString() for error in window_component.errors()]
    qml_engine.globalObject().setProperty("testBridge", qml_engine.newQObject(bridge))
    qml_engine.globalObject().setProperty("testWindow", qml_engine.newQObject(window))
    QGuiApplication.processEvents()
    yield window, bridge
    window.deleteLater()
    # Destroy the QML tree before its required bridge, including deferred loaders.
    from PySide6.QtCore import QCoreApplication, QEvent

    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    bridge.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    qml_engine.warnings.disconnect(collect_warnings)
    assert not warnings, "\n".join(warnings)


def _evaluate(engine, script):
    result = engine.evaluate(script)
    assert not result.isError(), result.toString()
    return result.toVariant()


SETTINGS_CATEGORIES = ("phone", "notifications", "calls", "security", "plugins", "about")


_FIND_JS = """(function find(item, name) {
    const children = item.children || [];
    for (let index = 0; index < children.length; ++index) {
        const child = children[index];
        if (child.objectName === name) return child;
        const found = find(child, name);
        if (found) return found;
    }
    return null;
})"""


def _visual_find(root, name, engine=None):
    """Search the visual tree in QML: Repeater delegates are not QObject
    children, and walking it from Python trips over unwrappable types."""
    engine = engine or qmlEngine(root)
    finder = engine.evaluate(_FIND_JS)
    found = finder.call([engine.newQObject(root), name])
    return found.toQObject() if found.isQObject() else None


def _find(window, name):
    obj = window.findChild(QObject, name)
    page = window.findChild(QObject, "phoneSettingsPage")
    if obj is None and page is not None:
        obj = _visual_find(page, name)
    return obj


def _settings_object(window, name):
    """Find a control on the settings page, opening the category that holds it."""
    obj = _find(window, name)
    page = window.findChild(QObject, "phoneSettingsPage")
    if obj is None and page is not None:
        for category in SETTINGS_CATEGORIES:
            page.setProperty("category", category)
            QGuiApplication.processEvents()
            obj = _find(window, name)
            if obj is not None:
                break
    assert obj is not None, name
    return obj


def _click_control(window, control):
    # Exercise Qt's actual pointer handling, including toggling checked before
    # clicked. Emitting clicked directly would miss a broken checkbox binding.
    QTest.qWait(30)
    assert control.property("visible") and control.property("enabled")
    point = control.mapToScene(QPointF(10, control.height() / 2)).toPoint()
    assert 0 <= point.x() < window.width() and 0 <= point.y() < window.height()
    QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, point)
    QGuiApplication.processEvents()


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("compatibility", [False, True])
@pytest.mark.parametrize("replace", [False, True])
def test_qt_pairing_checkbox_reaches_the_helper(
    qml_engine, monkeypatch, explicit, compatibility, replace,
):
    from types import SimpleNamespace

    from blueferry.qt.controller import BridgeController
    from blueferry.setup_client import ConfigurationState

    calls, operations = [], []
    setup = SimpleNamespace(complete_isolated=lambda mac, **options: calls.append((mac, options)))
    bridge = BridgeController(backend=object(), setup=setup, subscribe=False, autostart=False)
    bridge._setup_loaded = True
    bridge._compatibility = {
        "adapter": "hci1", "notifications_supported": not compatibility,
        "explicit_pairing_default": not explicit,
    }
    bridge._devices = [{"mac": "NEW", "display_name": "Phone", "paired": False,
                        "adapter_path": "/org/bluez/hci1"}]
    bridge._configuration = ConfigurationState.from_dict({
        "configured": False, "saved": replace, "mac": "OLD" if replace else "",
    })
    monkeypatch.setattr(bridge, "_run", lambda operation, *_args, **_kwargs: operations.append(operation))
    component = _component(qml_engine, "src/blueferry/qt/qml/Main.qml")
    window = component.createWithInitialProperties({"bridge": bridge, "height": 1600})
    assert window is not None
    try:
        bridge.setupLoadedChanged.emit()
        QGuiApplication.processEvents()
        checkbox = _settings_object(window, "explicitPairingCheckBox")
        assert checkbox.property("checked") is not explicit
        _click_control(window, checkbox)
        assert checkbox.property("checked") is explicit
        # A capability refresh must preserve the manual choice.
        bridge._compatibility = {**bridge._compatibility, "powered": True}
        bridge.compatibilityChanged.emit()
        assert checkbox.property("checked") is explicit
        # Opening the settings page also asks for the feature switches.
        before = len(operations)
        _click_control(window, _settings_object(window, "pairPhoneButton"))
        if replace:
            dialog = _settings_object(window, "replaceTargetDialog")
            assert dialog.property("visible")
            qml_engine.globalObject().setProperty("pairingDialog", qml_engine.newQObject(dialog))
            _evaluate(qml_engine, "pairingDialog.customFooterActions[0].trigger()")
        assert len(operations) == before + 1
        operations[-1]()
        assert len(calls) == 1
        mac, options = calls[0]
        assert mac == "NEW"
        assert options["adapter"] == "hci1"
        assert options.get("explicit_pairing", False) is explicit
        assert options.get("compatibility_mode", False) is compatibility
        assert options["replace_saved_mac"] == ("OLD" if replace else "")
    finally:
        window.close()
        window.deleteLater()
        QGuiApplication.processEvents()
        bridge.deleteLater()


@pytest.mark.parametrize("policy,storage_state,label", [
    ("encrypted", "locked", "Locked"), ("plaintext", "ready", "Available"),
    ("none", "disabled", "Disabled"),
])
def test_qt_storage_label_reports_unavailability_after_failed_reads(settings_window, policy, storage_state, label):
    from blueferry.conversation_state import ConversationSnapshot, ConversationState
    from blueferry.models import BackendStatus

    window, bridge = settings_window
    status_label = _settings_object(window, "storageStatusLabel")
    state = ConversationState()
    healthy = BackendStatus(daemon=True, storage_policy=policy, storage_state=storage_state)
    labels = []
    for snapshot in (
        ConversationSnapshot(status=healthy),
        ConversationSnapshot(status_error="status timed out"),
        ConversationSnapshot(status=healthy),
    ):
        state.apply_snapshot(snapshot)
        bridge.setProperty("status", state.status.to_dict())
        labels.append(status_label.property("text"))
    assert labels == [label, "Unavailable", label]


def test_qt_warns_about_a_suspect_le_bond_only_when_the_backend_reports_it(
    qml_engine, settings_window,
):
    from blueferry.models import BackendStatus

    window, bridge = settings_window
    message = _settings_object(window, "leBondSuspectMessage")
    assert "forget" in message.property("text")
    assert _evaluate(qml_engine, "testWindow.leBondSuspect()") is False

    bridge.setProperty(
        "status",
        BackendStatus.from_dict({"daemon": True, "le_bond_suspect": True}).to_dict(),
    )
    assert _evaluate(qml_engine, "testWindow.leBondSuspect()") is True

    bridge.setProperty(
        "status",
        BackendStatus.from_dict({"daemon": True, "le_bond_suspect": "true"}).to_dict(),
    )
    assert _evaluate(qml_engine, "testWindow.leBondSuspect()") is False


def test_reconnect_banner_shows_only_states_that_need_attention(
    qml_engine, settings_window,
):
    from PySide6.QtCore import QMetaObject

    window, bridge = settings_window
    message = _settings_object(window, "reconnectMessage")
    bridge.setProperty("configured", True)
    bridge.setProperty("reconnect", {
        "available": True, "offered": True, "attention": False,
        "hint": "Reconnecting automatically.",
    })
    QGuiApplication.processEvents()
    assert message.property("visible") is False
    bridge.setProperty("reconnect", {
        "available": True, "offered": True, "attention": True,
        "hint": "Waiting for the iPhone; next automatic try in 9 min.",
    })
    QGuiApplication.processEvents()
    assert message.property("visible") is True
    assert "9 min" in message.property("text")
    action = _settings_object(window, "reconnectAction")
    QMetaObject.invokeMethod(action, "trigger")
    assert _evaluate(qml_engine, "testBridge.calls.map(c => c.method)")[-1] == "reconnectPhone"




def _dial_dialog(qml_engine, window):
    dialog = _settings_object(window, "dialLinkDialog")
    qml_engine.globalObject().setProperty("dialLinkDialog", qml_engine.newQObject(dialog))
    text = _settings_object(window, "dialLinkText")
    qml_engine.globalObject().setProperty("dialLinkText", qml_engine.newQObject(text))
    return dialog, text


def _visible_dial_actions(qml_engine):
    return _evaluate(qml_engine, '''dialLinkDialog.customFooterActions
        .filter(action => action.visible).map(action => action.text)''')


def _bridge_methods(qml_engine):
    return _evaluate(qml_engine, "testBridge.calls.map(call => call.method)")


def _wait_closed(dialog):
    for _ in range(100):
        if not dialog.property("visible"):
            return
        QTest.qWait(20)
    raise AssertionError("dialog did not close")


def _close_dial_dialog(dialog):
    assert QMetaObject.invokeMethod(dialog, "close")
    _wait_closed(dialog)


def test_phone_link_dialog_asks_before_dialing_and_explains_disabled_calls(
    qml_engine, settings_window,
):
    window, bridge = settings_window
    dialog, text = _dial_dialog(qml_engine, window)
    number_label = _settings_object(window, "dialLinkNumber")

    # Calls off: say so and offer the settings instead of a Call button.
    bridge.setProperty("status", {"daemon": True, "calls_enabled": False})
    _evaluate(qml_engine, "testBridge.dialLinkRequested('+15550100000', '')")
    QGuiApplication.processEvents()
    assert dialog.property("visible") is True
    assert dialog.property("canCall") is False
    assert "turned off" in text.property("text")
    assert "+15550100000" in text.property("text")
    assert _visible_dial_actions(qml_engine) == ["Open iPhone Settings"]
    _evaluate(qml_engine, "dialLinkDialog.customFooterActions[0].trigger()")
    QGuiApplication.processEvents()
    assert _evaluate(qml_engine, "testWindow.iphoneSettingsPage !== null") is True
    _evaluate(qml_engine, "testWindow.closePhoneSettings()")
    _wait_closed(dialog)

    # Calls ready: a known contact is named, the number is shown, and only
    # the Call action dials.
    bridge.setProperty("contactNames", {"+15550100000": "<b>Test Contact</b>"})
    bridge.setProperty("status", {"daemon": True, "calls_enabled": True, "calls_state": "ready"})
    _evaluate(qml_engine, "testBridge.dialLinkRequested('+15550100000', '')")
    QGuiApplication.processEvents()
    assert dialog.property("canCall") is True
    assert dialog.property("title") == "Place Call?"
    assert text.property("text") == "Call <b>Test Contact</b> from your iPhone?"
    assert _evaluate(qml_engine, "dialLinkText.textFormat") == 0  # Text.PlainText
    assert number_label.property("visible") is True
    assert number_label.property("text") == "+15550100000"
    assert _visible_dial_actions(qml_engine) == ["Call"]
    assert "dialCall" not in _bridge_methods(qml_engine)
    _evaluate(qml_engine, "dialLinkDialog.customFooterActions[1].trigger()")
    assert _evaluate(qml_engine, "testBridge.calls[testBridge.calls.length - 1]") == {
        "method": "dialCall", "args": ["+15550100000"],
    }
    _wait_closed(dialog)

    # Invalid and emergency links never offer a call.
    for number, problem, expected in (
        ("", "invalid", "does not contain"), ("112", "emergency", "Emergency"),
    ):
        _evaluate(qml_engine, f"testBridge.dialLinkRequested('{number}', '{problem}')")
        QGuiApplication.processEvents()
        assert expected in text.property("text")
        assert _visible_dial_actions(qml_engine) == []
        assert dialog.property("canCall") is False
        _close_dial_dialog(dialog)
    assert _bridge_methods(qml_engine).count("dialCall") == 1


def test_phone_link_dialog_waits_for_the_first_status(qml_engine, settings_window):
    window, bridge = settings_window
    dialog, text = _dial_dialog(qml_engine, window)
    # A link that started the client arrives before the status: no flash of
    # "turned off" with a settings button that then becomes Call.
    _evaluate(qml_engine, "testBridge.dialLinkRequested('+15550100000', '')")
    QGuiApplication.processEvents()
    assert dialog.property("visible") is False
    bridge.setProperty("status", {"daemon": True, "calls_enabled": True, "calls_state": "ready"})
    QGuiApplication.processEvents()
    assert dialog.property("visible") is True
    assert _visible_dial_actions(qml_engine) == ["Call"]
    _close_dial_dialog(dialog)

    # A failed status read still answers, as unreachable.
    bridge.setProperty("status", {})
    _evaluate(qml_engine, "testBridge.dialLinkRequested('+15550100000', '')")
    QGuiApplication.processEvents()
    assert dialog.property("visible") is False
    bridge.setProperty("status", {"daemon": False})
    QGuiApplication.processEvents()
    assert "not reachable" in text.property("text")
    assert _visible_dial_actions(qml_engine) == ["Open iPhone Settings"]
    _close_dial_dialog(dialog)


def test_phone_link_dialog_never_turns_a_button_into_call(qml_engine, settings_window):
    window, bridge = settings_window
    dialog, text = _dial_dialog(qml_engine, window)
    bridge.setProperty("status", {"daemon": True, "calls_enabled": True, "calls_state": "connecting"})
    _evaluate(qml_engine, "testBridge.dialLinkRequested('+15550100000', '')")
    QGuiApplication.processEvents()
    assert "still connecting" in text.property("text")
    assert _visible_dial_actions(qml_engine) == ["Open iPhone Settings"]
    # Calls become ready while the dialog is open: the offer stays as shown.
    bridge.setProperty("status", {"daemon": True, "calls_enabled": True, "calls_state": "ready"})
    QGuiApplication.processEvents()
    assert _visible_dial_actions(qml_engine) == ["Open iPhone Settings"]
    assert dialog.property("canCall") is False
    _close_dial_dialog(dialog)

    # The reverse only takes Call away.
    _evaluate(qml_engine, "testBridge.dialLinkRequested('+15550100000', '')")
    QGuiApplication.processEvents()
    assert _visible_dial_actions(qml_engine) == ["Call"]
    bridge.setProperty("status", {"daemon": True, "calls_enabled": True, "calls_state": "bluez_conflict"})
    QGuiApplication.processEvents()
    assert _visible_dial_actions(qml_engine) == []
    assert "(bluez_conflict)" in text.property("text")
    # A stale trigger cannot dial either.
    _evaluate(qml_engine, "dialLinkDialog.customFooterActions[1].trigger()")
    assert "dialCall" not in _bridge_methods(qml_engine)
    _wait_closed(dialog)


def test_a_second_phone_link_never_replaces_the_number_in_an_open_dialog(
    qml_engine, settings_window,
):
    window, bridge = settings_window
    dialog, text = _dial_dialog(qml_engine, window)
    bridge.setProperty("status", {"daemon": True, "calls_enabled": True, "calls_state": "ready"})
    _evaluate(qml_engine, "testBridge.dialLinkRequested('+15550100000', '')")
    QGuiApplication.processEvents()
    assert dialog.property("number") == "+15550100000"
    # Any session process can send a link; the open dialog is closed first.
    _evaluate(qml_engine, "testBridge.dialLinkRequested('+15550100001', '')")
    QGuiApplication.processEvents()
    assert dialog.property("number") == "+15550100000"
    for _ in range(100):
        if dialog.property("number") == "+15550100001" and dialog.property("visible"):
            break
        QTest.qWait(20)
    assert dialog.property("number") == "+15550100001"
    assert "+15550100001" in text.property("text")
    assert dialog.property("visible") is True
    assert "dialCall" not in _bridge_methods(qml_engine)
    _close_dial_dialog(dialog)


def test_optional_phone_status_indicator_appears_only_with_known_values(
    qml_engine, settings_window,
):
    window, bridge = settings_window
    # Default status (calls off, or oFono without values): nothing is loaded.
    assert window.findChild(QObject, "phoneStatusIndicator") is None
    bridge.setProperty("status", {
        "calls_enabled": True,
        "phone_battery_level": None,
        "phone_signal_strength": None,
    })
    QGuiApplication.processEvents()
    assert window.findChild(QObject, "phoneStatusIndicator") is None

    bridge.setProperty("status", {
        "calls_enabled": True,
        "phone_battery_level": 40, "phone_battery_source": "hfp",
        "phone_signal_strength": 80,
        "phone_network_name": "Sunrise",
        "phone_network_status": "roaming",
    })
    QGuiApplication.processEvents()
    indicator = _settings_object(window, "phoneStatusIndicator")
    assert _settings_object(window, "phoneBatteryLabel").property("text") == "40 %"
    assert indicator.property("batteryIconName") == "battery-040"
    assert indicator.property("signalIconName") == "network-mobile-80"
    assert indicator.property("summary") == (
        "iPhone battery about 40 % · Signal 80 % · Sunrise (roaming)"
    )

    # The operator name is remote text: the tooltip must not render HTML.
    bridge.setProperty("status", {
        "calls_enabled": True,
        "phone_battery_level": 40, "phone_battery_source": "hfp",
        "phone_network_name": "<b>Sun</b>",
        "phone_network_status": "registered",
    })
    QGuiApplication.processEvents()
    tool_tip = indicator.findChild(QObject, "phoneStatusToolTip")
    assert tool_tip is not None
    content = tool_tip.property("contentItem")
    label = content.findChild(QObject, "phoneStatusToolTipLabel")
    assert label is not None
    qml_engine.globalObject().setProperty("phoneToolTipLabel", qml_engine.newQObject(label))
    # Qt::PlainText is 0 (AutoText, the style default, is 2).
    assert _evaluate(qml_engine, "phoneToolTipLabel.textFormat") == int(Qt.TextFormat.PlainText.value)
    # Text.Wrap is 4, like the style's own tooltip label.
    assert _evaluate(qml_engine, "phoneToolTipLabel.wrapMode") == 4
    assert label.property("text") == "iPhone battery about 40 % · <b>Sun</b>"

    # A long name wraps inside the style's 14-grid-unit width cap.
    bridge.setProperty("status", {
        "calls_enabled": True,
        "phone_battery_level": 40, "phone_battery_source": "hfp",
        "phone_network_name": "Very Long Operator Name " * 3,
        "phone_network_status": "registered",
    })
    QGuiApplication.processEvents()
    cap = label.property("maxTextWidth")
    assert cap > 0
    assert content.property("implicitWidth") <= cap
    assert label.property("contentWidth") <= cap
    assert label.property("lineCount") > 1

    # Signal only: the battery parts hide, the indicator stays.
    bridge.setProperty("status", {"calls_enabled": True, "phone_signal_strength": 20})
    QGuiApplication.processEvents()
    assert _settings_object(window, "phoneBatteryLabel").property("visible") is False
    assert indicator.property("batteryIconName") == ""
    assert indicator.property("summary") == "Signal 20 %"

    bridge.setProperty("status", {})
    QGuiApplication.processEvents()
    # The Loader releases its item with deleteLater().
    QGuiApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert window.findChild(QObject, "phoneStatusIndicator") is None


def test_phone_settings_first_run_and_reopening_keep_the_page_alive(qml_engine, settings_window):
    window, bridge = settings_window
    assert window.property("iphoneSettingsPage") is None
    assert _evaluate(qml_engine, "testBridge.calls") == []
    bridge.setProperty("setupLoaded", True)
    QGuiApplication.processEvents()
    page = window.property("iphoneSettingsPage")
    assert page is not None
    assert page.objectName() == "phoneSettingsPage"
    assert window.property("firstRunRedirected") is True
    for _ in range(3):
        assert QMetaObject.invokeMethod(page, "closeRequested")
        QGuiApplication.processEvents()
        assert window.property("iphoneSettingsPage") is None
        assert QMetaObject.invokeMethod(window, "openPhoneSettings")
        QGuiApplication.processEvents()
        assert window.property("iphoneSettingsPage") == page
    assert _evaluate(qml_engine, "testBridge.calls") == []


def test_proximity_lock_settings_appear_only_for_supporting_daemons(qml_engine, settings_window):
    window, bridge = settings_window
    bridge.setProperty("setupLoaded", True)
    QGuiApplication.processEvents()
    loader = _settings_object(window, "proximityLockLoader")
    bridge.setProperty("status", {"daemon": True})
    assert loader.property("active") is False

    bridge.setProperty("status", {
        "daemon": True,
        "proximity_lock": "disabled",
        "proximity_lock_enabled": False,
        "proximity_lock_grace_sec": 90,
    })
    QGuiApplication.processEvents()
    assert loader.property("active") is True
    warning = _settings_object(window, "proximityLockWarning")
    assert "never unlocks" in warning.property("text")
    assert "not a security feature" in warning.property("text")
    assert _settings_object(window, "proximityLockGraceSpinBox").property("value") == 90
    checkbox = _settings_object(window, "proximityLockCheckBox")
    assert checkbox.property("checked") is False
    assert _evaluate(qml_engine, "testBridge.calls.filter(c => c.method === 'setProximityLock')") == []

    assert QMetaObject.invokeMethod(checkbox, "toggle")
    assert QMetaObject.invokeMethod(checkbox, "clicked")
    assert _evaluate(
        qml_engine, "testBridge.calls.filter(c => c.method === 'setProximityLock')"
    ) == [{"method": "setProximityLock", "args": [True, 90]}]


def test_call_history_opt_in_checkbox_appears_only_for_supporting_daemons(
    qml_engine, settings_window,
):
    window, bridge = settings_window
    bridge.setProperty("setupLoaded", True)
    QGuiApplication.processEvents()
    loader = _settings_object(window, "callHistoryLoader")
    bridge.setProperty("status", {"daemon": True})
    assert loader.property("active") is False

    bridge.setProperty("status", {
        "daemon": True,
        "call_history_enabled": False,
        "missed_call_notifications": True,
    })
    QGuiApplication.processEvents()
    assert loader.property("active") is True
    note = _settings_object(window, "callHistoryPrivacyNote")
    assert "erases" in note.property("text")
    checkbox = _settings_object(window, "callHistoryCheckBox")
    popups = _settings_object(window, "missedCallPopupsCheckBox")
    assert checkbox.property("checked") is False
    assert popups.property("enabled") is False, "popups need the opt-in first"

    assert QMetaObject.invokeMethod(checkbox, "toggle")
    assert QMetaObject.invokeMethod(checkbox, "clicked")
    assert _evaluate(
        qml_engine, "testBridge.calls.filter(c => c.method === 'setCallHistory')"
    ) == [{"method": "setCallHistory", "args": [True, True]}]

    bridge.setProperty("status", {
        "daemon": True,
        "call_history_enabled": True,
        "missed_call_notifications": True,
    })
    QGuiApplication.processEvents()
    assert popups.property("enabled") is True
    assert QMetaObject.invokeMethod(popups, "toggle")
    assert QMetaObject.invokeMethod(popups, "clicked")
    assert _evaluate(
        qml_engine, "testBridge.calls.filter(c => c.method === 'setCallHistory')"
    )[-1] == {"method": "setCallHistory", "args": [True, False]}


def test_click_rule_editor_appears_only_for_supporting_daemons(qml_engine, settings_window):
    window, bridge = settings_window
    bridge.setProperty("setupLoaded", True)
    QGuiApplication.processEvents()
    loader = _settings_object(window, "notificationOpenMapLoader")
    # A backend from before click rules: no revision key, no method calls.
    bridge.setProperty("status", {"daemon": True, "notification_policy": "all"})
    QGuiApplication.processEvents()
    assert loader.property("active") is False
    bridge.setProperty("status", {
        "daemon": True, "notification_policy": "messages", "notification_open_map_revision": 3,
    })
    QGuiApplication.processEvents()
    assert loader.property("active") is False
    loads = "testBridge.calls.filter(c => c.method === 'loadNotificationOpenMap').length"
    assert _evaluate(qml_engine, loads) == 0

    bridge.setProperty("status", {
        "daemon": True, "notification_policy": "all", "notification_open_map_revision": 3,
    })
    QGuiApplication.processEvents()
    assert loader.property("active") is True
    assert _evaluate(qml_engine, loads) == 1


def test_media_control_setting_appears_only_for_supporting_daemons(qml_engine, settings_window):
    window, bridge = settings_window
    bridge.setProperty("setupLoaded", True)
    QGuiApplication.processEvents()
    loader = _settings_object(window, "mediaControlLoader")
    bridge.setProperty("status", {"daemon": True})
    assert loader.property("active") is False

    bridge.setProperty("status", {
        "daemon": True, "media_control_enabled": False, "media_control_available": False,
    })
    QGuiApplication.processEvents()
    assert loader.property("active") is True
    checkbox = _settings_object(window, "mediaControlCheckBox")
    assert checkbox.property("checked") is False
    assert _settings_object(window, "mediaControlStateLabel").property("text") == "Off"

    assert QMetaObject.invokeMethod(checkbox, "toggle")
    assert QMetaObject.invokeMethod(checkbox, "clicked")
    assert _evaluate(
        qml_engine, "testBridge.calls.filter(c => c.method === 'setMediaControl')"
    ) == [{"method": "setMediaControl", "args": [True]}]

    bridge.setProperty("status", {
        "daemon": True, "media_control_enabled": True, "media_control_available": True,
    })
    assert "Connected" in _settings_object(window, "mediaControlStateLabel").property("text")


def test_phone_audio_switch_appears_only_for_supporting_daemons(qml_engine, settings_window):
    window, bridge = settings_window
    bridge.setProperty("setupLoaded", True)
    bridge.setProperty("status", {"daemon": True})
    QGuiApplication.processEvents()
    loader = _settings_object(window, "phoneAudioLoader")
    assert loader.property("active") is False

    bridge.setProperty("status", {"daemon": True, "phone_audio_route": "phone"})
    bridge.setProperty("phoneAudio", {
        "supported": True, "available": True, "onPc": False, "pending": False,
        "hint": "Playing on the iPhone.", "playing": "phone", "mismatch": False,
    })
    QGuiApplication.processEvents()
    assert loader.property("active") is True
    switch = _settings_object(window, "phoneAudioSwitch")
    assert switch.property("checked") is False and switch.property("enabled") is True
    assert switch.property("subtitle") == "Playing on the iPhone."

    assert QMetaObject.invokeMethod(switch, "toggle")
    assert QMetaObject.invokeMethod(switch, "toggled")
    assert _evaluate(
        qml_engine, "testBridge.calls.filter(c => c.method === 'setPhoneAudioRoute')"
    ) == [{"method": "setPhoneAudioRoute", "args": ["pc"]}]
    # The switch follows the reported route, not the click.
    assert switch.property("checked") is False

    # Unavailable (policy, disconnected, pending): greyed out with the reason.
    bridge.setProperty("phoneAudio", {
        "supported": True, "available": False, "onPc": False, "pending": False,
        "hint": "Available while the iPhone is connected.", "playing": "", "mismatch": False,
    })
    QGuiApplication.processEvents()
    assert switch.property("enabled") is False
    assert "connected" in switch.property("subtitle")
    # Checked follows the reported route, not the click.
    assert switch.property("checked") is False


def test_ancs_actions_checkbox_is_opt_in_and_gated(qml_engine, settings_window):
    window, bridge = settings_window
    bridge.setProperty("setupLoaded", True)
    bridge.setProperty("status", {"daemon": True, "notification_policy": "all"})
    QGuiApplication.processEvents()
    checkbox = _settings_object(window, "ancsActionsCheckBox")
    # Daemons without the preference key do not support the setting.
    assert checkbox.property("visible") is False

    status = {
        "daemon": True,
        "notification_policy": "messages",
        "ancs_actions_preference": False,
        "notification_content_shown": True,
    }
    bridge.setProperty("status", status)
    QGuiApplication.processEvents()
    assert checkbox.property("visible") is True
    assert checkbox.property("checked") is False
    # Actions only apply to "All iPhone Notifications".
    assert checkbox.property("enabled") is False

    bridge.setProperty("status", {**status, "notification_policy": "all",
                                  "notification_content_shown": False})
    QGuiApplication.processEvents()
    assert checkbox.property("enabled") is False

    # A saved "on" can always be switched off, under any policy.
    bridge.setProperty("status", {**status, "ancs_actions_preference": True})
    QGuiApplication.processEvents()
    assert checkbox.property("enabled") is True

    bridge.setProperty("status", {**status, "notification_policy": "all"})
    QGuiApplication.processEvents()
    assert checkbox.property("enabled") is True
    assert QMetaObject.invokeMethod(checkbox, "toggle")
    assert QMetaObject.invokeMethod(checkbox, "clicked")
    assert _evaluate(
        qml_engine, "testBridge.calls.filter(c => c.method === 'setAncsNotificationActions')"
    ) == [{"method": "setAncsNotificationActions", "args": [True]}]


def test_mirror_removals_checkbox_is_opt_in_and_needs_a_supporting_daemon(
    qml_engine, settings_window,
):
    window, bridge = settings_window
    bridge.setProperty("setupLoaded", True)
    bridge.setProperty("status", {"daemon": True, "notification_policy": "all"})
    QGuiApplication.processEvents()
    checkbox = _settings_object(window, "mirrorRemovalsCheckBox")
    # Daemons that don't report the key do not support the setting.
    assert checkbox.property("visible") is False

    status = {
        "daemon": True,
        "notification_policy": "all",
        "mirror_iphone_removals": False,
    }
    bridge.setProperty("status", status)
    QGuiApplication.processEvents()
    assert checkbox.property("visible") is True
    assert checkbox.property("checked") is False
    assert checkbox.property("enabled") is True

    bridge.setProperty("busy", True)
    QGuiApplication.processEvents()
    assert checkbox.property("enabled") is False
    bridge.setProperty("busy", False)
    bridge.setProperty("status", {**status, "daemon": False})
    QGuiApplication.processEvents()
    assert checkbox.property("enabled") is False

    bridge.setProperty("status", {**status, "mirror_iphone_removals": True})
    QGuiApplication.processEvents()
    assert checkbox.property("checked") is True
    assert QMetaObject.invokeMethod(checkbox, "toggle")
    assert QMetaObject.invokeMethod(checkbox, "clicked")
    assert _evaluate(
        qml_engine,
        "testBridge.calls.filter(c => c.method === 'setMirrorNotificationRemovals')",
    ) == [{"method": "setMirrorNotificationRemovals", "args": [False]}]


def test_mpris_checkbox_needs_media_control_and_a_supporting_daemon(qml_engine, settings_window):
    window, bridge = settings_window
    bridge.setProperty("setupLoaded", True)
    bridge.setProperty("status", {"daemon": True, "media_control_enabled": False})
    QGuiApplication.processEvents()
    mpris = _settings_object(window, "mprisPlayerCheckBox")
    assert mpris.property("visible") is False

    bridge.setProperty("status", {
        "daemon": True, "media_control_enabled": False, "media_mpris_enabled": False,
    })
    assert mpris.property("visible") is True
    assert mpris.property("enabled") is False
    assert "every application" in _settings_object(window, "mprisPlayerWarning").property("text")

    bridge.setProperty("status", {
        "daemon": True, "media_control_enabled": True, "media_mpris_enabled": False,
    })
    assert mpris.property("enabled") is True
    assert QMetaObject.invokeMethod(mpris, "toggle")
    assert QMetaObject.invokeMethod(mpris, "clicked")
    assert _evaluate(
        qml_engine, "testBridge.calls.filter(c => c.method === 'setMprisPlayer')"
    ) == [{"method": "setMprisPlayer", "args": [True]}]


def test_proximity_grace_edit_survives_a_status_refresh_before_saving(qml_engine):
    component = QQmlComponent(qml_engine)
    component.setData(b'''
        import QtQuick
        QtObject {
            property var status: ({})
            property bool busy: false
            property var calls: []
            function setProximityLock(enabled, grace) {
                calls = calls.concat([[enabled, grace]]);
            }
        }
    ''', QUrl())
    bridge = component.create()
    bridge.setProperty("status", {
        "daemon": True, "proximity_lock": "armed",
        "proximity_lock_enabled": True, "proximity_lock_grace_sec": 60,
    })
    settings_component = _component(
        qml_engine, "src/blueferry/qt/qml/ProximityLockSettings.qml"
    )
    settings = settings_component.createWithInitialProperties({"bridge": bridge})
    spin = settings.findChild(QObject, "proximityLockGraceSpinBox")
    assert spin.property("value") == 60

    assert QMetaObject.invokeMethod(spin, "increase")
    assert QMetaObject.invokeMethod(spin, "valueModified")
    # An unrelated StatusChanged refresh arrives inside the coalescing window.
    bridge.setProperty("status", {
        "daemon": True, "proximity_lock": "armed",
        "proximity_lock_enabled": True, "proximity_lock_grace_sec": 60,
    })
    assert spin.property("value") == 70
    QTest.qWait(1000)
    calls = bridge.property("calls")
    if hasattr(calls, "toVariant"):
        calls = calls.toVariant()
    assert calls == [[True, 70]]

    # After saving, the daemon's value is followed again.
    bridge.setProperty("status", {
        "daemon": True, "proximity_lock": "armed",
        "proximity_lock_enabled": True, "proximity_lock_grace_sec": 120,
    })
    assert spin.property("value") == 120
    settings.deleteLater()
    bridge.deleteLater()


@pytest.mark.parametrize("status,expected", [
    ({"proximity_lock": "armed"}, "Armed"),
    ({"proximity_lock": "grace", "proximity_lock_remaining_sec": 12}, "locking in 12 s"),
    ({"proximity_lock": "locked"}, "Locked the desktop"),
    ({"proximity_lock": "idle", "proximity_lock_inhibited": "sleep"}, "Paused"),
    ({"proximity_lock": "idle", "proximity_lock_inhibited": ""}, "Waiting"),
    ({"proximity_lock": "disabled"}, "Off"),
])
def test_proximity_lock_state_text(qml_engine, status, expected):
    component = QQmlComponent(qml_engine)
    component.setData(b'''
        import QtQuick
        QtObject {
            property var status: ({})
            property bool busy: false
            function setProximityLock(enabled, grace) {}
        }
    ''', QUrl())
    bridge = component.create()
    bridge.setProperty("status", {"daemon": True, **status})
    settings_component = _component(
        qml_engine, "src/blueferry/qt/qml/ProximityLockSettings.qml"
    )
    settings = settings_component.createWithInitialProperties({"bridge": bridge})
    assert settings is not None
    label = settings.findChild(QObject, "proximityLockStateLabel")
    assert expected in label.property("text")
    settings.deleteLater()
    bridge.deleteLater()


def test_phone_settings_pairing_uses_loaded_selection_and_busy_state(qml_engine, settings_window):
    window, bridge = settings_window
    bridge.setProperty("setupLoaded", True)
    button = _settings_object(window, "pairPhoneButton")
    assert not button.property("enabled")
    bridge.setProperty("devices", [{"mac": "new-phone", "display_name": "Phone", "paired": False}])
    assert not button.property("enabled")
    bridge.setProperty("compatibility", {
        "available": False, "hardware_supported": False,
        "messages_supported": False, "notifications_supported": False,
        "adapter": "hci1", "adapters": [
            {"name": "hci0", "label": "First"}, {"name": "hci1", "label": "Second"},
        ],
    })
    bridge.setProperty("compatibilityLoaded", True)
    assert button.property("enabled")
    assert _settings_object(window, "adapterSelector").property("currentIndex") == 1
    unverified = bridge.property("compatibility")
    if hasattr(unverified, "toVariant"):
        unverified = unverified.toVariant()
    bridge.setProperty("compatibility", {
        **unverified, "available": True, "pairing_ready": False,
        "issue": "Incompatible Bluetooth adapter: missing Bluetooth LE",
    })
    assert not button.property("enabled")
    bridge.setProperty("compatibility", unverified)
    assert button.property("enabled")
    bridge.setProperty("busy", True)
    assert not button.property("enabled")
    bridge.setProperty("busy", False)
    assert QMetaObject.invokeMethod(button, "clicked")
    assert _evaluate(qml_engine, "testBridge.calls") == [
        {"method": "completePairing", "args": ["new-phone", True, False]},
    ]
    bridge.setProperty("devices", [])
    assert not button.property("enabled")


def test_qt_explicit_pairing_default_preserves_per_adapter_choices(qml_engine, settings_window):
    window, bridge = settings_window
    bridge.setProperty("setupLoaded", True)
    bridge.setProperty("compatibilityLoaded", True)
    bridge.setProperty("devices", [{"mac": "NEW", "display_name": "Phone", "paired": False}])
    checkbox = _settings_object(window, "explicitPairingCheckBox")
    pair = _settings_object(window, "pairPhoneButton")
    rtl = {"adapter": "hci1", "explicit_pairing_default": True}
    other = {"adapter": "hci0", "explicit_pairing_default": False}
    bridge.setProperty("compatibility", rtl)
    assert checkbox.property("checked")
    QMetaObject.invokeMethod(pair, "clicked")
    assert _evaluate(qml_engine, "testBridge.calls[0].args") == ["NEW", True, True]

    QMetaObject.invokeMethod(checkbox, "toggle")
    QMetaObject.invokeMethod(checkbox, "clicked")
    bridge.setProperty("compatibility", {**rtl, "powered": True})
    assert not checkbox.property("checked")
    QMetaObject.invokeMethod(pair, "clicked")
    assert _evaluate(qml_engine, "testBridge.calls[1].args") == ["NEW", True, False]

    bridge.setProperty("compatibility", other)
    assert not checkbox.property("checked")
    QMetaObject.invokeMethod(checkbox, "toggle")
    QMetaObject.invokeMethod(checkbox, "clicked")
    bridge.setProperty("compatibility", rtl)
    assert not checkbox.property("checked")
    bridge.setProperty("compatibility", other)
    assert checkbox.property("checked")


@pytest.mark.parametrize("confirm", [False, True])
def test_phone_replacement_keeps_the_confirmed_targets_across_refresh(qml_engine, settings_window, confirm):
    window, bridge = settings_window
    bridge.setProperty("setupLoaded", True)
    bridge.setProperty("targetSaved", True)
    bridge.setProperty("configuredMac", "old-phone")
    bridge.setProperty("compatibilityLoaded", True)
    bridge.setProperty("devices", [{"mac": "new-phone", "display_name": "Phone", "paired": False}])
    assert QMetaObject.invokeMethod(_settings_object(window, "pairPhoneButton"), "clicked")
    dialog = _settings_object(window, "replaceTargetDialog")
    assert dialog.property("visible")
    assert _evaluate(qml_engine, "testBridge.calls") == []
    bridge.setProperty("configuredMac", "changed-saved-phone")
    bridge.setProperty("devices", [{"mac": "changed-selection", "display_name": "Other", "paired": False}])
    qml_engine.globalObject().setProperty("testDialog", qml_engine.newQObject(dialog))
    if confirm:
        _evaluate(qml_engine, "testDialog.customFooterActions[0].trigger()")
        assert _evaluate(qml_engine, "testBridge.calls") == [
            {"method": "replaceAndPair", "args": ["old-phone", "new-phone", True, False]},
        ]
    else:
        assert QMetaObject.invokeMethod(dialog, "close")
        assert _evaluate(qml_engine, "testBridge.calls") == []


@pytest.mark.parametrize("confirm", [False, True])
def test_storage_change_waits_for_confirmation(qml_engine, settings_window, confirm):
    window, bridge = settings_window
    bridge.setProperty("status", {"daemon": True, "storage_policy": "encrypted", "storage_state": "ready"})
    bridge.setProperty("setupLoaded", True)
    selector = _settings_object(window, "storagePolicySelector")
    selector.setProperty("currentIndex", 2)
    qml_engine.globalObject().setProperty("testSelector", qml_engine.newQObject(selector))
    _evaluate(qml_engine, "testSelector.activated(2)")
    dialog = _settings_object(window, "storageChangeDialog")
    assert dialog.property("visible")
    assert _evaluate(qml_engine, "testBridge.calls") == []
    qml_engine.globalObject().setProperty("testDialog", qml_engine.newQObject(dialog))
    if confirm:
        _evaluate(qml_engine, "testDialog.customFooterActions[0].trigger()")
    else:
        assert QMetaObject.invokeMethod(dialog, "close")
    calls = _evaluate(qml_engine, "testBridge.calls")
    changes = [call for call in calls if call["method"] == "setStoragePolicy"]
    assert changes == ([{"method": "setStoragePolicy", "args": ["none"]}] if confirm else [])


@pytest.mark.parametrize("approve", [False, True])
def test_pairing_confirmation_remains_available_after_settings_close(qml_engine, settings_window, approve):
    window, bridge = settings_window
    bridge.setProperty("setupLoaded", True)
    assert QMetaObject.invokeMethod(window, "closePhoneSettings")
    assert window.property("iphoneSettingsPage") is None
    _evaluate(qml_engine, 'testBridge.pairingConfirmationRequested("123456")')
    dialog = _settings_object(window, "pairingConfirmationDialog")
    assert dialog.property("visible")
    assert "123456" in dialog.property("subtitle")
    assert _evaluate(qml_engine, "testBridge.calls") == []
    qml_engine.globalObject().setProperty("testDialog", qml_engine.newQObject(dialog))
    _evaluate(qml_engine, f"testDialog.customFooterActions[{int(approve)}].trigger()")
    assert _evaluate(qml_engine, "testBridge.calls") == [
        {"method": "answerPairingConfirmation", "args": [approve]},
    ]


@pytest.mark.parametrize("relative_path", [
    "data/quickshell/shell.qml", "src/blueferry/qt/qml/Main.qml",
])
def test_read_sync_requires_visible_active_conversation(qml_engine, relative_path):
    """Execute the shipped handlers with inert windows and backend recorders."""
    component = _component(qml_engine, "src/blueferry/qt/qml/ConversationLogic.qml")
    logic = component.create()
    assert logic is not None
    qml_engine.globalObject().setProperty("conversationLogic", qml_engine.newQObject(logic))
    source = (ROOT / relative_path).read_text()
    functions = _qml_functions(source, (
        "threadByKey", "selectedThread", "threadIsUnread", "markSelectedThreadRead",
    ))
    script = '''(function() {
        var calls = [];
        var threads = [{key: "one", unread: true}];
        var selectedThreadKey = "one";
        var bridge = {threads: threads, markThreadRead: function(key) { calls.push(key); }};
        var backendBridge = {request: function(method, args) { calls.push(args.thread_key); }};
        var window = {visible: true};
        var applicationSurface = {Window: {active: true}};
        var phoneSettingsVisible = false;
        var root = {visible: true, active: true, iphoneSettingsPage: null};
    ''' + functions + '''
        window.visible = root.visible = false;
        markSelectedThreadRead();
        window.visible = root.visible = true;
        applicationSurface.Window.active = root.active = false;
        markSelectedThreadRead();
        applicationSurface.Window.active = root.active = true;
        phoneSettingsVisible = true;
        root.iphoneSettingsPage = {};
        markSelectedThreadRead();
        if (calls.length) throw new Error("Hidden conversation was marked read");
        phoneSettingsVisible = false;
        root.iphoneSettingsPage = null;
        markSelectedThreadRead();
        return JSON.stringify(calls);
    })()'''
    result = qml_engine.evaluate(script)
    assert not result.isError(), result.toString()
    assert result.toString() == '["one"]'
    logic.deleteLater()


class _MessageDialogBridge(QObject):
    from PySide6.QtCore import Signal

    messageSendSucceeded = Signal(str, str)
    groupConfirmationRequested = Signal(str, str, str)

    @Property(bool, constant=True)
    def busy(self):
        return False

    @Property("QVariantList", constant=True)
    def contactResults(self):
        return []

    @Slot(str)
    def findContacts(self, _query):
        pass


def test_new_message_dialog_retains_edited_draft_after_earlier_send(qml_engine):
    bridge = _MessageDialogBridge()
    component = _component(qml_engine, "src/blueferry/qt/qml/NewMessageDialog.qml")
    dialog = component.createWithInitialProperties({
        "bridge": bridge, "recipient": "alice@example.com", "body": "original draft",
    })
    assert dialog is not None
    dialog.setProperty("body", "new draft")
    bridge.messageSendSucceeded.emit("alice@example.com", "original draft")
    assert dialog.property("body") == "new draft"
    assert dialog.property("recipient") == "alice@example.com"
    bridge.messageSendSucceeded.emit("alice@example.com", "new draft")
    assert dialog.property("body") == ""
    assert dialog.property("recipient") == ""
    dialog.deleteLater()


def test_group_confirmation_component_shows_literal_recipients(qml_engine):
    bridge = _MessageDialogBridge()
    component = _component(qml_engine, "src/blueferry/qt/qml/GroupConfirmationDialog.qml")
    dialog = component.createWithInitialProperties({"bridge": bridge})
    assert dialog is not None
    bridge.groupConfirmationRequested.emit("group:test", "draft", "<alice>\nbob@example.com")
    assert dialog.property("threadKey") == "group:test"
    assert dialog.property("draft") == "draft"
    assert dialog.property("subtitle") == "<span>&lt;alice&gt;<br>bob@example.com</span>"
    dialog.deleteLater()


def test_quickshell_search_coalesces_queries_and_ignores_superseded_replies(qml_engine):
    source = (ROOT / "data/quickshell/BackendBridge.qml").read_text()
    functions = _qml_functions(source, ("requestLatest", "cancelLatest", "handleLine"))
    result = qml_engine.evaluate('''(function() {
        var latestRequests = {}, latestMethods = {}, sent = [], replies = [], errors = [];
        var nextId = 1;
        function request(method, args) { sent.push(args.query); return nextId++; }
        function response(method, id, result) { replies.push(result); }
        function failure(method, id, error) { errors.push(error); }
        function eventReceived() {}
    ''' + functions + '''
        requestLatest("contacts", {query: "A"});
        requestLatest("contacts", {query: "B"});
        requestLatest("contacts", {query: "A"});
        handleLine(JSON.stringify({method: "contacts", id: 1, ok: false, error: "old failure"}));
        if (replies.length || errors.length || sent.join() !== "A,A") throw new Error("stale search");
        handleLine(JSON.stringify({method: "contacts", id: 2, ok: true, result: "latest A"}));
        requestLatest("contacts", {query: "B"});
        cancelLatest("contacts");
        handleLine(JSON.stringify({method: "contacts", id: 3, ok: true, result: "cancelled"}));
        requestLatest("contacts", {query: "C"});
        handleLine(JSON.stringify({method: "", id: 0, ok: false, error: "bridge failed"}));
        handleLine(JSON.stringify({method: "contacts", id: 4, ok: true, result: "before failure"}));
        requestLatest("contacts", {query: "D"});
        handleLine(JSON.stringify({method: "contacts", id: 5, ok: true, result: "recovered"}));
        return JSON.stringify({replies: replies, errors: errors, sent: sent});
    })()''')
    assert not result.isError(), result.toString()
    import json

    assert json.loads(result.toString()) == {
        "replies": ["latest A", "recovered"], "errors": ["bridge failed"],
        "sent": ["A", "A", "B", "C", "D"],
    }


@pytest.mark.parametrize("text", [
    "  alice@example.com\n\n+15551111111\nalice@example.com  ",
    "alice\rbob\r\nalice\u2028carol\u0085dave\v\ffrank",
    "", " \n\t ",
])
def test_roster_parsing_matches_between_python_and_qml(qml_engine, text):
    import json

    from blueferry.recipients import participant_lines

    component = _component(qml_engine, "src/blueferry/qt/qml/ConversationLogic.qml")
    logic = component.create()
    assert logic is not None
    qml_engine.globalObject().setProperty("logic", qml_engine.newQObject(logic))
    result = qml_engine.evaluate("JSON.stringify(logic.participantLines(" + json.dumps(text) + "))")
    assert not result.isError(), result.toString()
    assert json.loads(result.toString()) == participant_lines(text)
    logic.deleteLater()


def test_qml_conversation_decisions_match_python_state(qml_engine):
    import json

    from blueferry.conversation_state import ConversationSnapshot, ConversationState
    from blueferry.models import Thread

    component = _component(qml_engine, "src/blueferry/qt/qml/ConversationLogic.qml")
    logic = component.create()
    assert logic is not None
    qml_engine.globalObject().setProperty("logic", qml_engine.newQObject(logic))
    threads = [{
        "key": "group:one", "is_group": True, "roster_changed": True,
        "unexpected_sender": "Alice", "recipients": ["b@example.com", "a@example.com", "b@example.com"],
        "messages": [{"handle": "message", "outgoing": False, "read": False}],
    }]
    state = ConversationState()
    state.apply_snapshot(ConversationSnapshot(None, tuple(Thread.from_dict(t) for t in threads)))
    threads = [thread.to_dict() for thread in state.threads]
    thread = state.next_roster_warning()
    assert thread is not None
    result = qml_engine.evaluate('''(function() {
        const threads = ''' + json.dumps(threads) + ''';
        return JSON.stringify([
            logic.threadByKey(threads, "group:one").key,
            logic.threadForMessage(threads, "message").key,
            logic.threadIsUnread(threads[0]),
            logic.groupSignature(threads[0]),
            logic.nextRosterWarning(threads).key,
            logic.nextRosterWarning(threads)
        ]);
    })()''')
    assert not result.isError(), result.toString()
    assert json.loads(result.toString()) == [
        thread.key, thread.key, thread.unread, thread.confirmation_token, thread.key, None,
    ]
    assert state.next_roster_warning() is None
    logic.deleteLater()


@pytest.fixture
def quickshell_setup(qml_engine):
    """The real controller has no IO; record requests without a transport."""
    component = _component(qml_engine, "data/quickshell/SetupController.qml")
    controller = component.create()
    assert controller is not None
    qml_engine.globalObject().setProperty("setup", qml_engine.newQObject(controller))
    result = qml_engine.evaluate('''
        var requests = [], cancelled = [], answers = [], configuration = [], resets = 0;
        setup.executeRequested.connect((id, kind, argv, interactive) =>
          requests.push({id: id, kind: kind, argv: argv, interactive: interactive}));
        setup.cancelRequested.connect(id => cancelled.push(id));
        setup.inputRequested.connect((id, text) => answers.push({id: id, text: text}));
        setup.configurationUpdated.connect(value => configuration.push(value));
        setup.historyReset.connect(() => resets++);
        function check(value, message) { if (!value) throw new Error(message); }
        function reply(kind, data, code) {
          setup.finish(setup.pending[kind].id, kind, code || 0, JSON.stringify(data), "");
        }
        function ready() {
          setup.loadCompatibility("hci0");
          reply("compatibility", {notifications_supported: false, adapter: "hci0"});
          reply("devices", [{mac: "NEW", name: "Phone", adapter_path: "/org/bluez/hci0"}]);
        }
    ''')
    assert not result.isError(), result.toString()
    yield controller
    controller.deleteLater()
    QGuiApplication.processEvents()


@pytest.mark.parametrize(
    ("status", "expected", "absent"),
    [
        ({}, "Try running sudo systemctl restart bluetooth.service,", "rc-service"),
        (
            {"bluetooth_restart_command": "sudo rc-service bluetooth restart"},
            "Try running sudo rc-service bluetooth restart,",
            "systemctl",
        ),
        (
            {"bluetooth_restart_command": ""},
            "Try restarting the Bluetooth service,",
            "systemctl",
        ),
    ],
)
def test_quickshell_ancs_hint_uses_the_bridge_restart_command(
    qml_engine, quickshell_setup, status, expected, absent,
):
    theme = _component(qml_engine, "data/quickshell/ThemePalette.qml").create()
    component = _component(qml_engine, "data/quickshell/PhoneSettingsPage.qml")
    page = component.createWithInitialProperties({
        "ferryTheme": theme, "setup": quickshell_setup, "status": status,
    })
    assert page is not None
    try:
        qml_engine.globalObject().setProperty("hintPage", qml_engine.newQObject(page))
        hint = _evaluate(qml_engine, "hintPage.ancsUnavailableHint()")
        assert expected in hint
        assert absent not in hint
    finally:
        page.deleteLater()


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("compatibility", [False, True])
@pytest.mark.parametrize("replace", [False, True])
def test_quickshell_pairing_checkbox_reaches_the_helper(
    qml_engine, quickshell_setup, explicit, compatibility, replace,
):
    theme_component = _component(qml_engine, "data/quickshell/ThemePalette.qml")
    theme = theme_component.create()
    component = _component(qml_engine, "data/quickshell/PhoneSettingsPage.qml")
    page = component.createWithInitialProperties({
        "ferryTheme": theme, "setup": quickshell_setup, "status": {},
        "width": 620, "height": 1600,
    })
    assert page is not None
    window = QQuickWindow()
    window.resize(620, 1600)
    page.setParentItem(window.contentItem())
    window.show()
    try:
        _evaluate(qml_engine, '''
            setup.loadCompatibility("hci1");
            reply("compatibility", ''' + __import__("json").dumps({
                "adapter": "hci1", "notifications_supported": not compatibility,
                "explicit_pairing_default": not explicit,
            }) + ''');
            reply("devices", [{mac:"NEW",adapter_path:"/org/bluez/hci1"}]);
        ''')
        quickshell_setup.setProperty("targetSaved", replace)
        quickshell_setup.setProperty("configuredMac", "OLD" if replace else "")
        checkbox = _settings_object(page, "explicitPairingCheckBox")
        assert checkbox.property("checked") is not explicit
        _click_control(window, checkbox)
        assert checkbox.property("checked") is explicit
        # Scanning after selecting the option must leave it selected.
        _evaluate(qml_engine, '''setup.loadDevices(true);
            reply("devices", [{mac:"NEW",adapter_path:"/org/bluez/hci1"}]);''')
        assert checkbox.property("checked") is explicit
        _click_control(window, _settings_object(page, "pairPhoneButton"))
        if replace:
            _evaluate(qml_engine, "setup.confirmReplacement()")
        command = _evaluate(qml_engine, "requests[requests.length - 1].argv")
        assert command[:3] == ["/usr/bin/blueferry", "pairing-complete", "NEW"]
        assert ("--explicit-pairing" in command) is explicit
        assert ("--compatibility-mode" in command) is compatibility
        assert command[command.index("--adapter") + 1] == "hci1"
        assert ("--replace-saved-mac" in command) is replace
        if replace:
            assert command[command.index("--replace-saved-mac") + 1] == "OLD"
    finally:
        window.close()
        page.deleteLater()
        window.deleteLater()
        QGuiApplication.processEvents()
        theme.deleteLater()


@pytest.mark.parametrize("scenario", [
    # A timer may request configuration before Component.onCompleted calls
    # start(). Either order must start locally, then resume live bond checks.
    '''setup.refreshConfiguration(); const first = setup.pending.configuration.id;
       setup.start();
       check(setup.pending.configuration.id === first, "startup replaced initial read");
       check(requests[0].argv.includes("--saved-only"), "startup waits for Bluetooth");
       reply("configuration", {configured:true,saved:true,bonded:null,mac:"SAVED"});
       check(setup.configured && !setup.bondStateKnown, "unknown bond treated as lost");
       setup.refreshConfiguration();
       check(!requests[requests.length-1].argv.includes("--saved-only"), "live bond never checked");
       reply("configuration", {configured:false,saved:true,bonded:false,mac:"SAVED"});
       check(!setup.configured && setup.bondStateKnown, "removed bond ignored");''',
    # A separately upgraded Quickshell must work with an older backend too.
    '''setup.start(); const old = setup.pending.configuration.id;
       setup.finish(old,"configuration",2,"","No such option: --saved-only");
       check(setup.pending.configuration.id !== old, "missing fallback");
       check(!requests[requests.length-1].argv.includes("--saved-only"), "fallback repeats unknown option");
       reply("configuration", {configured:true,saved:true,bonded:true,mac:"SAVED"});
       check(setup.configured && !setup.configurationError, "older backend left unusable");''',
    # First install: missing saved target must still allow explicit pairing on
    # an unverified controller; empty/failed discovery must not enable Pair.
    '''setup.start();
       reply("configuration", {configured: false, saved: false});
       check(configuration.join() === "false", "first-run navigation");
       reply("compatibility", {notifications_supported: false, adapter: "hci0"});
       reply("devices", []);
       check(!setup.canPair && setup.selectedDeviceIndex === -1, "empty scan");
       ready(); check(setup.canPair, "unverified controller blocked pairing");
       setup.requestPairing();
       check(requests[requests.length-1].argv.includes("--compatibility-mode"), "compatibility flag");''',
    # Changing adapters must invalidate a running scan and its late response.
    '''ready(); setup.loadDevices(true); const old = setup.pending.devices.id;
       setup.loadCompatibility("hci1");
       setup.finish(old, "devices", 0, '[{"mac":"OLD","adapter_path":"/org/bluez/hci0"}]', "");
       check(!setup.pairingDevices.length && cancelled.includes(old), "stale adapter scan");
       reply("compatibility", {notifications_supported: true, adapter: "hci1"});
       reply("devices", [{mac:"WRONG",adapter_path:"/org/bluez/hci0"}, {mac:"RIGHT",adapter_path:"/org/bluez/hci1"}]);
       check(setup.selectedPairingDevice().mac === "RIGHT", "wrong adapter selected");''',
    '''ready(); setup.loadDevices(true); const old = setup.pending.devices.id;
       setup.cancelScan(); setup.loadDevices(true);
       setup.finish(old, "devices", 0, "[]", "");
       check(setup.scanning && setup.pairingDevices.length === 1, "cancelled result changed selection");
       reply("devices", [{mac:"NEW",name:"renamed",adapter_path:"/org/bluez/hci0"}]);
       check(setup.selectedPairingDevice().mac === "NEW" && !setup.scanning, "selection lost");''',
    # Mutation cancels config reads and clears an interactive prompt on failure.
    '''ready(); setup.refreshConfiguration(); const old = setup.pending.configuration.id;
       setup.requestPairing(); const pair = setup.pending.pair.id;
       setup.receiveLine(pair, "pair", '{"event":"confirmation","passkey":"012345"}');
       check(setup.pairingConfirmationPending && setup.pairingPasskey === "012345", "missing prompt");
       setup.finish(old, "configuration", 0, '{"configured":true,"saved":true,"mac":"STALE"}', "");
       check(setup.configuredMac === "", "old configuration accepted");
       reply("pair", {ok:false,error:"Rejected",report_path:"/tmp/fake-report"}, 1);
       check(!setup.pairing && !setup.pairingConfirmationPending && setup.pairingStatus === "Rejected", "failed pair stuck");
       setup.answerConfirmation(true);
       setup.receiveLine(pair,"pair",'{"event":"confirmation"}');
       check(!answers.length && !setup.pairingConfirmationPending, "answered stale prompt");
       reply("configuration", {configured:false,saved:false});
       check(setup.pairingIssueReport === "/tmp/fake-report", "failure report lost");''',
    '''ready(); setup.targetSaved = true; setup.configuredMac = "OLD";
       setup.setExplicitPairing(true); setup.requestPairing();
       check(!setup.pairing && setup.pendingReplacement, "replacement skipped confirmation");
       setup.configuredMac = "OTHER"; setup.adapterName = "hci9";
       setup.pairingDevices = [{mac:"OTHER-NEW"}]; setup.confirmReplacement();
       const argv = requests[requests.length-1].argv;
       check(argv[2] === "NEW" && argv[argv.indexOf("--replace-saved-mac")+1] === "OLD", "replacement target drift");
       check(argv[argv.indexOf("--adapter")+1] === "hci0" && argv.includes("--explicit-pairing"), "replacement options drift");''',
    '''ready(); setup.configured = true; setup.targetSaved = true;
       setup.configuredMac = "OLD"; setup.configuredAdapter = "hci1";
       setup.refreshConfiguration(); const old = setup.pending.configuration.id;
       setup.forgetPhone(); const id = setup.pending.forget.id;
       setup.receiveLine(id,"forget",'{"event":"confirmation","purpose":"forget"}');
       setup.answerConfirmation(false); setup.answerConfirmation(true);
       check(answers.length === 1 && answers[0].text === "no\\n", "duplicate confirmation");
       reply("forget",{ok:false,error:"Cancelled"},1);
       check(setup.configured && !resets, "failed forget erased history");
       setup.forgetPhone(); reply("forget",{ok:true});
       setup.finish(old,"configuration",0,'{"configured":true,"saved":true,"mac":"OLD"}', "");
       check(!setup.configured && setup.configuredMac === "" && resets === 1, "forgotten target restored");''',
    '''ready(); setup.requestPairing();
       reply("pair",{ok:true,device:{mac:"NEW",adapter_path:"/org/bluez/hci0"},ancs_enabled:false});
       check(setup.configured && setup.configuredAdapter === "hci0" && !setup.ancsEnabled, "pair success lost");
       const status = setup.pairingStatus; reply("devices",[]);
       check(setup.pairingStatus === status, "refresh hid setup instructions");''',
    '''ready(); setup.loadDevices(true);
       setup.finish(setup.pending.devices.id,"devices",0,"garbage","");
       check(!setup.scanning && setup.pairingStatus !== "Scanning for Bluetooth devices…", "malformed scan stuck");
       setup.activateBluetooth(); reply("activate",{ok:false,error:"Denied"},1);
       check(!setup.activating && !setup.bluezActive, "failed activation accepted");
       setup.activateBluetooth(); reply("activate",{ok:true});
       check(setup.pending.compatibility, "activation did not refresh capabilities");''',
    '''setup.refreshConfiguration();
       setup.finish(setup.pending.configuration.id,"configuration",-1,"","Could not start");
       setup.finish(setup.pending.configuration.id,"configuration",-1,"","Could not start");
       check(configuration.join() === "false" && setup.configurationError === "Could not start", "missing helper hidden");
       setup.refreshConfiguration(); reply("configuration",{configured:false,saved:false});
       check(!setup.configurationError, "configuration recovery retained error");
       ready(); setup.requestPairing();
       setup.finish(setup.pending.pair.id,"pair",0,"","");
       check(!setup.configured && !setup.pairing && setup.pairingStatus.includes("no result"), "empty success accepted");''',
    '''ready();
       function selectAdapter(adapter, defaultValue) {
         setup.loadCompatibility(adapter);
         reply("compatibility", {adapter:adapter, notifications_supported:false,
           explicit_pairing_default:defaultValue});
         reply("devices", [{mac:"NEW",adapter_path:"/org/bluez/"+adapter}]);
       }
       selectAdapter("hci1", true);
       check(setup.explicitPairing, "RTL8761BU did not default to explicit pairing");
       setup.requestPairing();
       check(requests[requests.length-1].argv.includes("--explicit-pairing"), "default not forwarded");
       reply("pair", {ok:false,error:"Cancelled"}, 1);
       setup.setExplicitPairing(false);
       selectAdapter("hci1", true);
       check(!setup.explicitPairing, "refresh overwrote manual opt-out");
       selectAdapter("hci0", false);
       check(!setup.explicitPairing, "default leaked to another adapter");
       setup.setExplicitPairing(true);
       selectAdapter("hci1", true);
       check(!setup.explicitPairing, "adapter switch lost opt-out");
       setup.requestPairing();
       check(!requests[requests.length-1].argv.includes("--explicit-pairing"), "manual opt-out ignored");''',
    '''ready(); setup.setExplicitPairing(true); setup.requestPairing();
       const first = setup.pending.pair.id;
       const connected = '{"event":"transports","map":true,"pbap":true,"ancs":false}';
       setup.receiveLine(first, "pair", connected);
       check(setup.pairingTransports.map && setup.pairingTransports.pbap, "live services missing");
       check(setup.pairing && !setup.configured, "progress completed pairing prematurely");
       check(setup.pairingStatus.includes("Messages and contacts are connected"), "progress text missing");
       setup.receiveLine(first, "pair", '{"event":"transports","map":false,"pbap":false}');
       check(setup.pairingTransports.map, "malformed progress accepted");
       reply("pair", {ok:false,error:"Cancelled"}, 1);
       setup.requestPairing();
       check(!setup.pairingTransports.map && !setup.pairingTransports.pbap, "new attempt kept old services");
       setup.receiveLine(first, "pair", connected);
       setup.receiveLine(setup.pending.pair.id, "forget", connected);
       check(!setup.pairingTransports.map, "stale or wrong-kind progress accepted");
       setup.receiveLine(setup.pending.pair.id, "pair", connected);
       setup.receiveLine(setup.pending.pair.id, "pair", '{"event":"transports","map":false,"pbap":false,"ancs":false}');
       check(!setup.pairingTransports.map && !setup.pairingTransports.pbap, "lost services stayed connected");''',
], ids=["saved-startup", "older-backend", "first-install", "adapter-switch", "cancel-scan",
        "failed-pair", "replacement-snapshot",
        "forget-confirmation", "pair-success", "failed-helpers", "missing-helper", "explicit-default",
        "live-transports"])
def test_quickshell_setup_responses(qml_engine, quickshell_setup, scenario):
    result = qml_engine.evaluate("(function() {" + scenario + "})()")
    assert not result.isError(), result.toString()


def test_quickshell_settings_bindings_and_unverified_pairing(qml_engine, quickshell_setup):
    warnings = []
    qml_engine.warnings.connect(warnings.extend)
    theme_component = _component(qml_engine, "data/quickshell/ThemePalette.qml")
    theme = theme_component.create()
    page_component = _component(qml_engine, "data/quickshell/PhoneSettingsPage.qml")
    page = page_component.createWithInitialProperties({
        "ferryTheme": theme, "setup": quickshell_setup, "status": {
            "notification_policy": "messages", "storage_policy": "encrypted",
            "contacts_only_notifications": False,
        }, "width": 620, "height": 650,
    })
    assert page is not None
    qml_engine.globalObject().setProperty("page", qml_engine.newQObject(page))
    result = qml_engine.evaluate("ready()")
    assert not result.isError(), result.toString()
    QGuiApplication.processEvents()
    pair = page.findChild(QObject, "pairPhoneButton")
    assert pair.property("enabled")
    _evaluate(qml_engine, '''
        setup.loadCompatibility("hci1");
        reply("compatibility", {adapter: "hci1", available: true, pairing_ready: false,
            hardware_supported: false, notifications_supported: false,
            issue: "Incompatible Bluetooth adapter: missing Bluetooth LE"});
        reply("devices", [{mac: "OLD", paired: true, adapter_path: "/org/bluez/hci1"}]);
        setup.compatibilityModeOverride = true;
        setup.setExplicitPairing(true);
        setup.requestPairing();
        check(!setup.pairing, "incompatible controller started pairing");
    ''')
    assert not pair.property("enabled")
    message = page.findChild(QObject, "hardwareCompatibilityMessage")
    assert message.property("visible")
    assert "Incompatible Bluetooth adapter" in message.property("text")
    _evaluate(qml_engine, '''
        setup.configured = true;
        page.status = Object.assign({}, page.status, {daemon: true});
    ''')
    heading = page.findChild(QObject, "iphoneSetupHeading")
    instructions = page.findChild(QObject, "iphoneSetupInstructions")
    assert not pair.property("visible")
    assert message.property("visible")
    assert not heading.property("visible")
    assert not instructions.property("visible")
    _evaluate(qml_engine, '''
        setup.loadCompatibility("hci0");
        setup.finish(setup.pending.compatibility.id, "compatibility", 1, "", "btmgmt timed out");
        setup.loadDevices(false);
        reply("devices", [{mac: "NEW", adapter_path: "/org/bluez/hci0"}]);
    ''')
    assert pair.property("enabled")
    assert not message.property("visible")
    assert heading.property("visible")
    assert instructions.property("visible")
    assert "Enable Show Message Notifications" in instructions.property("text")
    _evaluate(qml_engine, "setup.configured = false")
    assert not heading.property("visible")
    QMetaObject.invokeMethod(pair, "clicked")
    assert quickshell_setup.property("pairing")
    assert not pair.property("enabled")
    messages = page.findChild(QObject, "messagesConnection")
    contacts = page.findChild(QObject, "contactsConnection")
    notifications = page.findChild(QObject, "notificationsConnection")
    assert messages.property("value") == contacts.property("value") == "Unavailable"
    _evaluate(qml_engine, '''
        setup.receiveLine(setup.pending.pair.id, "pair",
            '{"event":"transports","map":false,"pbap":true,"ancs":false}');
    ''')
    assert contacts.property("value") == "Connected"
    assert messages.property("value") == "Unavailable"
    _evaluate(qml_engine, '''
        setup.receiveLine(setup.pending.pair.id, "pair",
            '{"event":"transports","map":true,"pbap":true,"ancs":false}');
    ''')
    assert messages.property("value") == contacts.property("value") == "Connected"
    assert notifications.property("value") == "Unavailable"
    assert quickshell_setup.property("pairing")
    result = qml_engine.evaluate('''reply("pair", {ok:false,error:"Cancelled"},1);
        setup.configured = true;''')
    assert not result.isError(), result.toString()
    # Once the helper exits, ordinary backend status owns the connection rows.
    assert messages.property("value") == contacts.property("value") == "Unavailable"
    _evaluate(qml_engine, '''page.status = Object.assign({}, page.status,
        {map: true, pbap: true, ancs: true});''')
    assert messages.property("value") == contacts.property("value") == "Connected"
    assert notifications.property("value") == "Connected"
    QGuiApplication.processEvents()
    qml_engine.warnings.disconnect(warnings.extend)
    assert not warnings, "\n".join(w.toString() for w in warnings)
    page.deleteLater()
    theme.deleteLater()


@pytest.mark.parametrize("colors", [
    {"background": "#1a1b26", "foreground": "#c0caf5", "accent": "#9ece6a"},
    {"background": "#fafafa", "foreground": "#242424", "accent": "#b00060"},
])
def test_quickshell_theme_keeps_blue_messages_and_opaque_window(qml_engine, colors):
    component = _component(qml_engine, "data/quickshell/ThemePalette.qml")
    theme = component.createWithInitialProperties({
        "quattroActive": True, "colors": colors,
        "shell": {"popups.background-alpha": "0.4"},
    })
    assert theme is not None
    assert theme.property("windowSurface").alphaF() == 1
    assert theme.property("panelRadius") == 0
    assert theme.property("controlRadius") == 0
    assert theme.property("fontFamily") == "monospace"
    assert theme.property("outgoingBubble").name() == "#245baf"
    bubble_component = _component(qml_engine, "data/quickshell/QuickshellMessageBubble.qml")
    bubble = bubble_component.createWithInitialProperties({
        "ferryTheme": theme, "message": {"body": "Blue in every theme", "outgoing": True},
        "availableWidth": 500, "availableHeight": 300, "showSender": False,
    })
    assert bubble is not None
    assert bubble.property("color").name() == "#245baf"
    assert bubble.findChild(QObject, "messageBody").property("color").name() == "#ffffff"
    bubble.deleteLater()
    theme.deleteLater()


@pytest.fixture
def quickshell_environment(tmp_path):
    # Non-login builders have no /run/user/<uid>. Quickshell needs a writable
    # runtime directory even with the offscreen platform and a private bus.
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    environment = dict(
        os.environ, QT_QPA_PLATFORM="offscreen", NO_AT_BRIDGE="1",
        XDG_RUNTIME_DIR=str(runtime),
    )
    environment.pop("WAYLAND_DISPLAY", None)
    return environment


@pytest.mark.private_dbus
def test_quickshell_setup_transport_streams_cancels_and_times_out(
    tmp_path, quickshell_environment,
):
    """Run actual Quickshell IO with inert Python children on the private bus."""
    import json
    import shutil
    import subprocess
    import sys

    executable = shutil.which("quickshell")
    if executable is None:
        pytest.skip("Quickshell is not installed")
    for name in ("SetupTransport.qml", "SetupJob.qml"):
        shutil.copyfile(ROOT / "data/quickshell" / name, tmp_path / name)
    interactive = [sys.executable, "-c", '''import sys,time
sys.stdout.write('{"event":'); sys.stdout.flush(); time.sleep(.05)
print('"confirmation"}',flush=True)
answer = input()
print('{"answer":"' + answer + '"}',flush=True)
print("diagnostic",file=sys.stderr)
sys.exit(7)
''']
    cancelled = [sys.executable, "-c", '''import time
print("ready",flush=True)
time.sleep(5)
''']
    missing = [str(tmp_path / "missing-helper")]
    # SetupTransport is exercised directly; no production backend or setup
    # command is reachable from this configuration.
    source = '''import QtQuick
import Quickshell
ShellRoot {
  id: root
  property int completed: 0
  property int prompts: 0
  property bool cancelled: false
  function check(ok, detail) { if (!ok) throw new Error(detail); }
  function done() {
    completed++;
    if (completed === 3) { console.log("BLUEFERRY_TRANSPORT_OK"); Qt.quit(); }
  }
  SetupTransport {
    id: transport
    onLineReceived: (id, kind, line) => {
      if (id === 1 && JSON.parse(line).event === "confirmation") {
        root.prompts++;
        transport.write(id, "yes\\n");
      } else if (id === 2) {
        transport.cancel(id);
        root.cancelled = true;
        cancelCheck.start();
      }
    }
    onFinished: (id, kind, code, output, diagnostic) => {
      root.check(id === 1 && code === 7, "cancelled request delivered or wrong exit code");
      root.check(root.prompts === 1 && output.includes('"answer":"yes"'), "streaming stdin exchange failed");
      root.check(diagnostic === "diagnostic", "stderr lost at exit");
      root.done();
    }
  }
  Component {
    id: missingJob
    SetupJob {
      requestId: 3; kind: "configuration"; timeoutMs: 150
      command: MISSING_COMMAND
      onFinished: (id, kind, code, output, diagnostic) => {
        root.check(code === -1 && diagnostic.includes("could not start"), "failed spawn stayed busy");
        root.done();
        destroy();
      }
    }
  }
  Timer {
    id: cancelCheck; interval: 300
    onTriggered: {
      root.check(root.cancelled && !transport.jobs[2], "cancelled helper retained");
      root.done();
    }
  }
  Component.onCompleted: {
    transport.execute(1, "pair", INTERACTIVE_COMMAND, true);
    transport.execute(2, "devices", CANCEL_COMMAND, true);
    const job = missingJob.createObject(root);
    job.start();
  }
}
'''.replace("INTERACTIVE_COMMAND", json.dumps(interactive)).replace(
        "CANCEL_COMMAND", json.dumps(cancelled)
    ).replace("MISSING_COMMAND", json.dumps(missing))
    config = tmp_path / "shell.qml"
    config.write_text(source)
    result = subprocess.run(
        [executable, "--path", str(config)], env=quickshell_environment,
        capture_output=True, text=True, timeout=10, check=False,
    )
    log = result.stdout + result.stderr
    assert result.returncode == 0, log
    assert "BLUEFERRY_TRANSPORT_OK" in log, log
    assert "ReferenceError" not in log and "TypeError" not in log, log


def test_quickshell_storage_cancel_keeps_the_status_binding(qml_engine, quickshell_setup):
    from PySide6.QtCore import Qt
    from PySide6.QtQuick import QQuickWindow
    from PySide6.QtTest import QTest

    theme_component = _component(qml_engine, "data/quickshell/ThemePalette.qml")
    theme = theme_component.create()
    component = _component(qml_engine, "data/quickshell/PhoneSettingsPage.qml")
    page = component.createWithInitialProperties({
        "ferryTheme": theme, "setup": quickshell_setup, "status": {
            "storage_policy": "encrypted", "contacts_only_notifications": False,
        }, "width": 640, "height": 1400,
    })
    assert page is not None
    quickshell_setup.setProperty("configured", True)
    window = QQuickWindow()
    window.resize(640, 1400)
    page.setParentItem(window.contentItem())
    window.show()
    QGuiApplication.processEvents()
    selector = page.findChild(QObject, "storagePolicySelector")
    assert selector is not None
    calls = []
    page.operationRequested.connect(lambda method, args: calls.append(method))
    # Real keyboard activation preserves the original QML binding before the
    # rejection path explicitly restores it.
    selector.forceActiveFocus()
    QTest.keyClick(window, Qt.Key_Down)
    assert selector.property("currentIndex") == 0
    assert not calls
    page.setProperty("status", {"storage_policy": "none", "contacts_only_notifications": False})
    QGuiApplication.processEvents()
    assert selector.property("currentIndex") == 2
    window.close()
    page.deleteLater()
    theme.deleteLater()


def test_quickshell_ancs_actions_checkbox_is_opt_in_and_gated(
    qml_engine, quickshell_setup,
):
    from PySide6.QtQuick import QQuickWindow

    theme_component = _component(qml_engine, "data/quickshell/ThemePalette.qml")
    theme = theme_component.create()
    component = _component(qml_engine, "data/quickshell/PhoneSettingsPage.qml")
    page = component.createWithInitialProperties({
        "ferryTheme": theme, "setup": quickshell_setup, "status": {
            "notification_policy": "all", "contacts_only_notifications": False,
        }, "width": 640, "height": 1400,
    })
    assert page is not None
    quickshell_setup.setProperty("configured", True)
    window = QQuickWindow()
    window.resize(640, 1400)
    page.setParentItem(window.contentItem())
    window.show()
    QGuiApplication.processEvents()
    checkbox = page.findChild(QObject, "ancsActionsCheckBox")
    assert checkbox is not None
    # Daemons without the preference key do not support the setting.
    assert checkbox.property("visible") is False

    status = {
        "notification_policy": "messages",
        "contacts_only_notifications": False,
        "ancs_actions_preference": False,
        "notification_content_shown": True,
    }
    page.setProperty("status", status)
    QGuiApplication.processEvents()
    assert checkbox.property("visible") is True
    assert checkbox.property("checked") is False
    # Actions only apply to "All iPhone notifications".
    assert checkbox.property("enabled") is False
    page.setProperty("status", {**status, "notification_policy": "all",
                                "notification_content_shown": False})
    QGuiApplication.processEvents()
    assert checkbox.property("enabled") is False
    page.setProperty("busy", {"ancsActions": True})
    page.setProperty("status", {**status, "notification_policy": "all"})
    QGuiApplication.processEvents()
    assert checkbox.property("enabled") is False
    page.setProperty("busy", {})
    QGuiApplication.processEvents()
    assert checkbox.property("enabled") is True
    # A saved "on" can always be switched off, under any policy.
    page.setProperty("status", {**status, "ancs_actions_preference": True})
    QGuiApplication.processEvents()
    assert checkbox.property("enabled") is True
    page.setProperty("status", {**status, "notification_policy": "all"})
    QGuiApplication.processEvents()

    calls = []
    page.operationRequested.connect(
        lambda method, args: calls.append((
            method, args.toVariant() if hasattr(args, "toVariant") else args,
        ))
    )
    assert QMetaObject.invokeMethod(checkbox, "toggle")
    assert QMetaObject.invokeMethod(checkbox, "clicked")
    assert calls == [("set_ancs_notification_actions", {"enabled": True})]
    window.close()
    page.deleteLater()
    theme.deleteLater()


def _quickshell_settings_page(qml_engine, quickshell_setup, status):
    from PySide6.QtQuick import QQuickWindow

    theme_component = _component(qml_engine, "data/quickshell/ThemePalette.qml")
    theme = theme_component.create()
    component = _component(qml_engine, "data/quickshell/PhoneSettingsPage.qml")
    page = component.createWithInitialProperties({
        "ferryTheme": theme, "setup": quickshell_setup, "status": status,
        "width": 640, "height": 1400,
    })
    assert page is not None
    quickshell_setup.setProperty("configured", True)
    window = QQuickWindow()
    window.resize(640, 1400)
    page.setParentItem(window.contentItem())
    window.show()
    QGuiApplication.processEvents()
    calls = []
    page.operationRequested.connect(
        lambda method, args: calls.append((
            method, args.toVariant() if hasattr(args, "toVariant") else args,
        ))
    )

    def close(_components=(theme_component, component)):
        # The default argument keeps the components, which own the page
        # and the theme, alive until the test is done.
        window.close()
        page.deleteLater()
        theme.deleteLater()

    return page, calls, close


def test_quickshell_away_lock_checkbox_is_opt_in_and_keeps_the_grace_period(
    qml_engine, quickshell_setup,
):
    base = {"notification_policy": "all", "contacts_only_notifications": False}
    page, calls, close = _quickshell_settings_page(qml_engine, quickshell_setup, base)
    checkbox = page.findChild(QObject, "proximityLockCheckBox")
    assert checkbox is not None
    # Daemons without the away-lock keys do not support the setting.
    assert checkbox.property("visible") is False

    status = {
        **base, "proximity_lock": "disabled",
        "proximity_lock_enabled": False, "proximity_lock_grace_sec": 120,
    }
    page.setProperty("status", status)
    QGuiApplication.processEvents()
    assert checkbox.property("visible") is True
    assert checkbox.property("checked") is False
    assert checkbox.property("enabled") is True
    page.setProperty("busy", {"proximityLock": True})
    QGuiApplication.processEvents()
    assert checkbox.property("enabled") is False
    page.setProperty("busy", {})
    page.setProperty("status", {**status, "proximity_lock_enabled": True})
    QGuiApplication.processEvents()
    assert checkbox.property("checked") is True
    page.setProperty("status", status)
    QGuiApplication.processEvents()

    assert QMetaObject.invokeMethod(checkbox, "toggle")
    assert QMetaObject.invokeMethod(checkbox, "clicked")
    assert calls == [("set_proximity_lock", {"enabled": True, "grace_seconds": 120})]
    close()


def test_quickshell_phone_calls_checkbox_is_opt_in(qml_engine, quickshell_setup):
    base = {"notification_policy": "all", "contacts_only_notifications": False}
    page, calls, close = _quickshell_settings_page(qml_engine, quickshell_setup, base)
    checkbox = page.findChild(QObject, "callsEnabledCheckBox")
    assert checkbox is not None
    # Daemons that do not report calls_enabled do not support the setting.
    assert checkbox.property("visible") is False

    page.setProperty("status", {**base, "calls_enabled": False})
    QGuiApplication.processEvents()
    assert checkbox.property("visible") is True
    assert checkbox.property("checked") is False
    assert checkbox.property("enabled") is True
    page.setProperty("busy", {"calls": True})
    QGuiApplication.processEvents()
    assert checkbox.property("enabled") is False
    page.setProperty("busy", {})
    page.setProperty("status", {**base, "calls_enabled": True})
    QGuiApplication.processEvents()
    assert checkbox.property("checked") is True

    assert QMetaObject.invokeMethod(checkbox, "toggle")
    assert QMetaObject.invokeMethod(checkbox, "clicked")
    assert calls == [("set_calls_enabled", {"enabled": False})]
    close()


def test_quickshell_saved_choice_is_not_undone_by_a_late_status(qml_engine) -> None:
    component = _component(qml_engine, "data/quickshell/SavedChoice.qml")
    choice = component.create()
    assert choice is not None
    qml_engine.globalObject().setProperty("choice", qml_engine.newQObject(choice))

    def state(script):
        return _evaluate(qml_engine, script + "; [choice.value, choice.busy]")

    assert state("choice.reported(true)") == [True, False]
    # While the save runs, a status still reporting the old value is ignored.
    assert state("choice.request(false); choice.reported(true)") == [False, True]
    # A status requested before the save finished is skipped once.
    assert state("choice.saved(false, true); choice.reported(true)") == [False, False]
    assert state("choice.reported(true)") == [True, False]
    # No status in flight: the next report is shown as it is.
    assert state("choice.request(false); choice.saved(false, false)") == [False, False]
    assert state("choice.reported(true)") == [True, False]
    # A failed save returns to what the daemon last reported.
    assert state("choice.request(false); choice.failed(true)") == [True, False]
    choice.deleteLater()


def test_quickshell_offers_the_contact_photo_opt_in_only_when_reported(
    qml_engine, quickshell_setup,
):
    theme_component = _component(qml_engine, "data/quickshell/ThemePalette.qml")
    theme = theme_component.create()
    component = _component(qml_engine, "data/quickshell/PhoneSettingsPage.qml")
    page = component.createWithInitialProperties({
        "ferryTheme": theme, "setup": quickshell_setup,
        "status": {"contacts_only_notifications": False},
    })
    assert page is not None
    quickshell_setup.setProperty("configured", True)
    checkbox = page.findChild(QObject, "contactPhotosCheckBox")
    assert checkbox is not None
    assert checkbox.property("visible") is False

    page.setProperty("status", {"contacts_only_notifications": False, "contact_photos": False})
    QGuiApplication.processEvents()
    assert checkbox.property("visible") is False

    page.setProperty("status", {
        "contacts_only_notifications": False, "contact_photos": False, "saved_opt_ins": True,
    })
    QGuiApplication.processEvents()
    assert checkbox.property("visible") is True
    assert checkbox.property("checked") is False
    calls = []
    page.operationRequested.connect(
        lambda method, args: calls.append(
            (method, args.toVariant() if hasattr(args, "toVariant") else args)
        )
    )
    assert QMetaObject.invokeMethod(checkbox, "toggle")
    assert QMetaObject.invokeMethod(checkbox, "clicked")
    assert calls == [("set_contact_photos", {"enabled": True})]

    page.setProperty("busy", {"photos": True})
    QGuiApplication.processEvents()
    assert checkbox.property("enabled") is False
    page.deleteLater()
    theme.deleteLater()


def test_quickshell_offers_the_one_time_code_opt_in_only_when_reported(
    qml_engine, quickshell_setup,
):
    theme_component = _component(qml_engine, "data/quickshell/ThemePalette.qml")
    theme = theme_component.create()
    component = _component(qml_engine, "data/quickshell/PhoneSettingsPage.qml")
    page = component.createWithInitialProperties({
        "ferryTheme": theme, "setup": quickshell_setup,
        "status": {"contacts_only_notifications": False},
    })
    assert page is not None
    quickshell_setup.setProperty("configured", True)
    checkbox = page.findChild(QObject, "otpAutocopyCheckBox")
    assert checkbox is not None
    assert checkbox.property("visible") is False

    page.setProperty("status", {"contacts_only_notifications": False, "otp_autocopy": False})
    QGuiApplication.processEvents()
    assert checkbox.property("visible") is False

    page.setProperty("status", {
        "contacts_only_notifications": False, "otp_autocopy": False, "saved_opt_ins": True,
    })
    QGuiApplication.processEvents()
    assert checkbox.property("visible") is True
    assert checkbox.property("checked") is False
    calls = []
    page.operationRequested.connect(
        lambda method, args: calls.append(
            (method, args.toVariant() if hasattr(args, "toVariant") else args)
        )
    )
    assert QMetaObject.invokeMethod(checkbox, "toggle")
    assert QMetaObject.invokeMethod(checkbox, "clicked")
    assert calls == [("set_otp_autocopy", {"enabled": True})]

    page.setProperty("busy", {"otp": True})
    QGuiApplication.processEvents()
    assert checkbox.property("enabled") is False
    page.deleteLater()
    theme.deleteLater()


@pytest.mark.private_dbus
def test_quickshell_keeps_the_restart_command_when_the_daemon_is_unavailable(
    tmp_path, quickshell_environment,
):
    """The bridge's host fact must outlive status resets in the real shell."""
    import shutil
    import subprocess

    executable = shutil.which("quickshell")
    if executable is None:
        pytest.skip("Quickshell is not installed")
    for source in (ROOT / "data/quickshell").glob("*.qml"):
        shutil.copyfile(source, tmp_path / source.name)
    shutil.copyfile(
        ROOT / "src/blueferry/qt/qml/ConversationLogic.qml",
        tmp_path / "ConversationLogic.qml",
    )
    (tmp_path / "Theme.qml").write_text("import QtQuick\nThemePalette {}\n")
    (tmp_path / "SetupTransport.qml").write_text('''import QtQuick
Item {
  signal lineReceived(int id, string kind, string line)
  signal finished(int id, string kind, int code, string output, string diagnostic)
  function execute(id, kind, command, interactive) {}
  function cancel(id) {}
  function write(id, text) {}
}
''')
    (tmp_path / "BackendBridge.qml").write_text('''import QtQuick
Item {
  property bool desktopClient: false
  property int nextId: 1
  signal response(string method, int requestId, var result)
  signal failure(string method, int requestId, string message)
  signal eventReceived(string name, var data)
  function request(method, args) { return nextId++; }
  function requestLatest(method, args) {}
  function cancelLatest(method) {}
}
''')
    config = tmp_path / "shell.qml"
    source = config.read_text()
    probe = r'''
  Timer {
    interval: 200; running: true
    onTriggered: {
      function check(value, message) { if (!value) throw new Error(message); }
      function hint() { return phoneSettingsPage.ancsUnavailableHint(); }
      try {
        check(hint().indexOf("systemctl") >= 0, "older bridges lost the systemd text");
        backendBridge.eventReceived("host",
          {bluetooth_restart_command: "sudo rc-service bluetooth restart"});
        check(hint().indexOf("sudo rc-service bluetooth restart") >= 0, "host event ignored");
        root.markStatusUnavailable("BlueFerry backend is unavailable");
        check(hint().indexOf("sudo rc-service bluetooth restart") >= 0,
          "unavailable daemon reset the restart command");
        setupController.historyReset();
        check(hint().indexOf("sudo rc-service bluetooth restart") >= 0,
          "history reset cleared the restart command");
        backendBridge.response("status", 1, {daemon: true, bluetooth_restart_command: ""});
        check(hint().indexOf("Try restarting the Bluetooth service") >= 0,
          "status reply did not update the restart command");
        backendBridge.response("status", 2, {daemon: true});
        check(hint().indexOf("Try restarting the Bluetooth service") >= 0,
          "status without the field cleared the restart command");
        console.log("BLUEFERRY_RESTART_HINT_OK");
      } catch (error) {
        console.error(error);
      }
      Qt.quit();
    }
  }
'''
    config.write_text(source[:source.rfind("}")] + probe + "}\n")
    result = subprocess.run(
        [executable, "--path", str(config)], env=quickshell_environment,
        capture_output=True, text=True, timeout=10, check=False,
    )
    log = result.stdout + result.stderr
    assert result.returncode == 0 and "BLUEFERRY_RESTART_HINT_OK" in log, log
    assert "ReferenceError" not in log and "TypeError" not in log, log


@pytest.mark.private_dbus
def test_quickshell_opens_thread_while_bluetooth_setup_is_pending(
    tmp_path, quickshell_environment,
):
    import shutil
    import subprocess

    executable = shutil.which("quickshell")
    if executable is None:
        pytest.skip("Quickshell is not installed")
    for source in (ROOT / "data/quickshell").glob("*.qml"):
        shutil.copyfile(source, tmp_path / source.name)
    shutil.copyfile(
        ROOT / "src/blueferry/qt/qml/ConversationLogic.qml", tmp_path / "ConversationLogic.qml",
    )
    (tmp_path / "Theme.qml").write_text("import QtQuick\nThemePalette {}\n")
    # Only the local read returns. All live Bluetooth checks remain pending;
    # neither transport can launch helpers or reach the operator's daemon.
    (tmp_path / "SetupTransport.qml").write_text('''import QtQuick
Item {
  id: transport
  signal lineReceived(int id, string kind, string line)
  signal finished(int id, string kind, int code, string output, string diagnostic)
  function execute(id, kind, command, interactive) {
    if (kind === "configuration" && command.indexOf("--saved-only") >= 0)
      Qt.callLater(function() {
        transport.finished(id, kind, 0,
          JSON.stringify({configured:true, saved:true, bonded:null, mac:"SAVED"}), "");
      });
  }
  function cancel(id) {}
  function write(id, text) {}
}
''')
    (tmp_path / "BackendBridge.qml").write_text('''import QtQuick
Item {
  id: bridge
  property bool desktopClient: false
  property int nextId: 1
  signal response(string method, int requestId, var result)
  signal failure(string method, int requestId, string message)
  signal eventReceived(string name, var data)
  function request(method, args) {
    const id = nextId++;
    if (method === "threads") Qt.callLater(function() {
      bridge.response(method, id, [{key:"direct:alice", name:"Alice", unread:false,
        is_group:false, recipients:["alice@example.com"], messages:[]}]);
    });
    else if (method === "status") Qt.callLater(function() {
      bridge.response(method, id, {daemon:true, storage_state:"ready"});
    });
    return id;
  }
  function requestLatest(method, args) {}
  function cancelLatest(method) {}
}
''')
    config = tmp_path / "shell.qml"
    source = config.read_text()
    probe = r'''
  Timer {
    interval: 300; running: true
    onTriggered: {
      function check(value, message) { if (!value) throw new Error(message); }
      try {
        check(root.threads.length === 1, "history blocked on Bluetooth");
        check(root.selectedThreadKey === "direct:alice", "requested thread not selected");
        check(root.pendingThreadKey === "", "requested thread not resolved");
        check(!root.phoneSettingsVisible, "conversation hidden by setup");
        check(setupController.pending.compatibility, "live Bluetooth was not pending");
        check(!setupController.bondStateKnown, "unchecked bond reported as verified");
        setupController.refreshConfiguration();
        check(setupController.pending.configuration, "live bond refresh not scheduled");
        root.reload();
        check(root.threadsBusy, "pending live check blocked refresh");
        const background = setupController.pending.configuration.id;
        setupController.finish(background, "configuration", -1, "", "Setup helper timed out");
        check(!root.phoneSettingsVisible, "failed background check hid the conversation");
        check(setupController.configured && setupController.configuredMac === "SAVED",
          "failed background check discarded the saved phone");
        check(setupController.configurationError === "Setup helper timed out",
          "failed background check lost its diagnostic");
        setupController.refreshConfiguration();
        setupController.finish(setupController.pending.configuration.id, "configuration", 0,
          JSON.stringify({configured:true, saved:true, bonded:true, mac:"SAVED"}), "");
        check(!root.phoneSettingsVisible && root.selectedThreadKey === "direct:alice",
          "recovery did not preserve the conversation");
        check(setupController.configurationError === "", "recovery kept an old diagnostic");
        setupController.refreshConfiguration();
        setupController.finish(setupController.pending.configuration.id, "configuration", 0,
          JSON.stringify({configured:false, saved:true, bonded:false, mac:"SAVED"}), "");
        check(root.phoneSettingsVisible && !setupController.configured,
          "a confirmed removed bond did not open setup");
        console.log("BLUEFERRY_OFFLINE_HISTORY_OK");
      } catch (error) { console.error(error); }
      Qt.quit();
    }
  }
'''
    config.write_text(source[:source.rfind("}")] + probe + "}\n")
    environment = quickshell_environment | {"BLUEFERRY_OPEN_THREAD_KEY": "direct:alice"}
    environment.pop("BLUEFERRY_OPEN_MESSAGE_HANDLE", None)
    result = subprocess.run(
        [executable, "--path", str(config)], env=environment,
        capture_output=True, text=True, timeout=10, check=False,
    )
    log = result.stdout + result.stderr
    assert result.returncode == 0 and "BLUEFERRY_OFFLINE_HISTORY_OK" in log, log
    assert "ReferenceError" not in log and "TypeError" not in log, log


@pytest.mark.private_dbus
def test_quickshell_thread_list_keyboard_navigation(tmp_path, quickshell_environment):
    """Send real clicks and keys to the shell, with no live transports."""
    import shutil
    import subprocess

    executable = shutil.which("quickshell")
    if executable is None:
        pytest.skip("Quickshell is not installed")
    for source in (ROOT / "data/quickshell").glob("*.qml"):
        shutil.copyfile(source, tmp_path / source.name)
    shutil.copyfile(
        ROOT / "src/blueferry/qt/qml/ConversationLogic.qml",
        tmp_path / "ConversationLogic.qml",
    )
    (tmp_path / "Theme.qml").write_text("import QtQuick\nThemePalette {}\n")
    (tmp_path / "SetupTransport.qml").write_text('''import QtQuick
Item {
  signal lineReceived(int id, string kind, string line)
  signal finished(int id, string kind, int code, string output, string diagnostic)
  function execute(id, kind, command, interactive) {}
  function cancel(id) {}
  function write(id, text) {}
}
''')
    (tmp_path / "BackendBridge.qml").write_text('''import QtQuick
Item {
  property bool desktopClient: false
  property int nextId: 1
  signal response(string method, int requestId, var result)
  signal failure(string method, int requestId, string message)
  signal eventReceived(string name, var data)
  function request(method, args) { return nextId++; }
  function requestLatest(method, args) {}
  function cancelLatest(method) {}
}
''')
    config = tmp_path / "shell.qml"
    source = config.read_text().replace("import QtQuick\n", "import QtQuick\nimport QtTest\n", 1)
    probe = r'''
  TestCase {
    parent: applicationSurface
    name: "ThreadNavigation"
    when: applicationSurface.Window.active

    function expect(actual, expected, detail) {
      if (actual !== expected) console.error(detail, "expected", expected, "got", actual);
      compare(actual, expected, detail);
    }

    function test_navigation() {
      setupController.configured = true;
      root.phoneSettingsVisible = false;
      const threads = [];
      for (let index = 0; index < 20; index++) {
        threads.push({key: "thread:" + index, name: "Person " + index,
          recipients: ["person" + index + "@example.com"], messages: [],
          reply_ready: true, is_group: false, starred: false});
      }
      root.threads = threads;
      wait(50);
      expect(root.selectedThreadKey, "", "loading threads must not select one");
      keyClick(Qt.Key_Down);
      expect(root.selectedThreadKey, "thread:0", "the list has initial keyboard focus");
      const row = threadList.itemAtIndex(2);
      verify(row !== null);
      mouseClick(row, row.width / 2, row.height / 2);
      compare(root.selectedThreadKey, "thread:2");
      keyClick(Qt.Key_Down);
      expect(root.selectedThreadKey, "thread:3", "Down opens the next conversation");
      keyClick(Qt.Key_Up);
      compare(root.selectedThreadKey, "thread:2", "Up opens the previous conversation");

      for (let index = 3; index < threads.length; index++) keyClick(Qt.Key_Down);
      compare(root.selectedThreadKey, "thread:19");
      wait(50);
      verify(threadList.contentY > 0, "keyboard selection must scroll the list");
      const last = threadList.itemAtIndex(19);
      verify(last !== null);
      const lastY = last.mapToItem(threadList, 0, 0).y;
      verify(lastY >= 0 && lastY + last.height <= threadList.height + 1,
        "the selected row must stay visible");
      keyClick(Qt.Key_Down);
      compare(root.selectedThreadKey, "thread:19", "Down stops at the end");
      for (let index = 0; index < threads.length; index++) keyClick(Qt.Key_Up);
      compare(root.selectedThreadKey, "thread:0", "Up stops at the beginning");

      // Refreshes can reorder rows (new messages, starring). Selection belongs
      // to a thread key, not the old row number or a transient model reset.
      root.selectedThreadKey = "thread:5";
      root.reload();
      backendBridge.response("threads", root.threadsRequestId, threads.slice().reverse());
      wait(50);
      compare(root.selectedThreadKey, "thread:5", "refresh preserves the selected thread");
      keyClick(Qt.Key_Down);
      expect(root.selectedThreadKey, "thread:4", "Down follows the refreshed order");

      // Arrow keys remain text-editing keys while the composer has focus,
      // including when a history refresh rebuilds the delegates.
      composer.text = "first line\nsecond line";
      composer.forceActiveFocus();
      const editor = composer.contentChildren[0];
      editor.cursorPosition = editor.length;
      root.reload();
      backendBridge.response("threads", root.threadsRequestId, threads);
      wait(50);
      keyClick(Qt.Key_Up);
      compare(root.selectedThreadKey, "thread:4");
      verify(editor.cursorPosition < "first line\n".length);
      keyClick(Qt.Key_Down);
      verify(editor.cursorPosition >= "first line\n".length);
      compare(composer.text, "first line\nsecond line");

      threadList.forceActiveFocus();
      root.reload();
      backendBridge.response("threads", root.threadsRequestId,
        threads.filter(thread => thread.key !== "thread:4"));
      wait(50);
      expect(root.selectedThreadKey, "", "removing the selected thread clears selection");
      keyClick(Qt.Key_Down);
      expect(root.selectedThreadKey, "thread:0", "navigation recovers after removal");

      newMessagePopup.open();
      wait(50);
      keyClick(Qt.Key_Down);
      expect(root.selectedThreadKey, "thread:0", "dialog keys must not navigate threads");
      newMessagePopup.close();
      wait(50);
      keyClick(Qt.Key_Down);
      expect(root.selectedThreadKey, "thread:1", "closing the dialog restores list focus");

      root.selectedThreadKey = "";
      root.threads = [];
      wait(50);
      keyClick(Qt.Key_Up);
      keyClick(Qt.Key_Down);
      compare(root.selectedThreadKey, "", "empty lists do not select anything");
      root.threads = threads;
      wait(50);
      keyClick(Qt.Key_Down);
      compare(root.selectedThreadKey, "thread:0", "Down starts at the first thread");
      root.selectedThreadKey = "";
      keyClick(Qt.Key_Up);
      compare(root.selectedThreadKey, "thread:19", "Up starts at the last thread");
      console.log("BLUEFERRY_THREAD_NAVIGATION_OK");
    }
  }
'''
    config.write_text(source[:source.rfind("}")] + probe + "}\n")
    environment = quickshell_environment.copy()
    environment.pop("BLUEFERRY_OPEN_THREAD_KEY", None)
    environment.pop("BLUEFERRY_OPEN_MESSAGE_HANDLE", None)
    result = subprocess.run(
        [executable, "--path", str(config)], env=environment,
        capture_output=True, text=True, timeout=15, check=False,
    )
    log = result.stdout + result.stderr
    assert result.returncode == 0 and "BLUEFERRY_THREAD_NAVIGATION_OK" in log, log
    assert "WARN scene:" not in log and "ReferenceError" not in log and "TypeError" not in log, log


def test_quickshell_composer_wraps_and_scrolls_when_resized(qml_engine):
    warnings = []

    def collect_warnings(errors):
        warnings.extend(error.toString() for error in errors)

    qml_engine.warnings.connect(collect_warnings)
    component = QQmlComponent(qml_engine)
    component.setData(b'''import QtQuick
import QtQuick.Layouts
import "."
Window {
  width: 320; height: 320; visible: true
  ThemePalette { id: theme }
  RowLayout {
    anchors.left: parent.left
    anchors.right: parent.right
    anchors.top: parent.top
    FerryMessageComposer {
      id: composer
      objectName: "composer"
      ferryTheme: theme
      Layout.fillWidth: true
      placeholderText: "Write a message..."
    }
  }
  readonly property real editorWidth: composer.contentChildren[0].width
  readonly property int lineCount: composer.contentChildren[0].lineCount
  readonly property real scrollHeight: composer.contentItem.contentHeight
  readonly property real maxComposerHeight: theme.scaled(144)
}''', QUrl.fromLocalFile(str(ROOT / "data/quickshell/composer-test.qml")))
    window = component.create()
    try:
        assert window is not None, [error.toString() for error in component.errors()]
        composer = window.findChild(QObject, "composer")
        assert composer is not None
        QTest.qWait(50)
        empty_height = composer.property("height")
        assert empty_height > 0
        assert window.property("editorWidth") == pytest.approx(composer.property("availableWidth"))

        composer.setProperty("text", "A long message that needs to wrap. " * 40)
        QTest.qWait(50)
        narrow_lines = window.property("lineCount")
        assert narrow_lines > 1 and composer.property("multiline")
        assert empty_height < composer.property("height") <= window.property("maxComposerHeight")
        assert window.property("scrollHeight") > composer.property("availableHeight")
        assert window.property("editorWidth") == pytest.approx(composer.property("availableWidth"))

        window.setWidth(640)
        QTest.qWait(50)
        assert window.property("editorWidth") == pytest.approx(composer.property("availableWidth"))
        assert composer.property("width") == pytest.approx(640)
        assert 1 < window.property("lineCount") < narrow_lines

        composer.setProperty("text", "")
        QTest.qWait(50)
        assert composer.property("height") == pytest.approx(empty_height)
        assert not composer.property("multiline")
    finally:
        if window is not None:
            window.deleteLater()
            QGuiApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        qml_engine.warnings.disconnect(collect_warnings)
    assert not warnings, "\n".join(warnings)


@pytest.mark.parametrize("late_after_refresh", [False, True])
@pytest.mark.private_dbus
@pytest.mark.parametrize("late_read", ["success", "failure"])
def test_quickshell_replies_use_the_saved_members_without_a_checkbox(
    tmp_path, quickshell_environment, late_read, late_after_refresh,
):
    """Exercise the real shell with both transports replaced before loading."""
    import shutil
    import subprocess

    executable = shutil.which("quickshell")
    if executable is None:
        pytest.skip("Quickshell is not installed")
    for source in (ROOT / "data/quickshell").glob("*.qml"):
        shutil.copyfile(source, tmp_path / source.name)
    shutil.copyfile(
        ROOT / "src/blueferry/qt/qml/ConversationLogic.qml",
        tmp_path / "ConversationLogic.qml",
    )
    (tmp_path / "Theme.qml").write_text("import QtQuick\nThemePalette {}\n")
    # No process can be started by either injected transport.
    (tmp_path / "SetupTransport.qml").write_text('''import QtQuick
Item {
  signal lineReceived(int id, string kind, string line)
  signal finished(int id, string kind, int code, string output, string diagnostic)
  function execute(id, kind, command, interactive) {}
  function cancel(id) {}
  function write(id, text) {}
}
''')
    (tmp_path / "BackendBridge.qml").write_text('''import QtQuick
Item {
  property bool desktopClient: false
  property var calls: []
  property int nextId: 1
  signal response(string method, int requestId, var result)
  signal failure(string method, int requestId, string message)
  signal eventReceived(string name, var data)
  function request(method, args) {
    const id = nextId++;
    calls.push({id: id, method: method, args: args});
    return id;
  }
  function requestLatest(method, args) {}
  function cancelLatest(method) {}
}
''')
    config = tmp_path / "shell.qml"
    source = config.read_text()
    probe = r'''
  Timer {
    interval: 200; running: true
    onTriggered: {
      function check(value, message) { if (!value) throw new Error(message); }
      function sends() { return backendBridge.calls.filter(call => call.method === "send_to_thread"); }
      function latestRead() { return backendBridge.calls.filter(call => call.method === "threads").slice(-1)[0].id; }
      try {
        setupController.configured = true;
        const recipients = ["alice@example.com", "bob@example.com"];
        const pending = {
          key: "crew", name: "Crew", group_origin: "named", is_group: true,
          recipients: recipients, messages: [], reply_ready: false,
          participants_required: true, group_confirmed: false,
          confirmation_token: "\nalice@example.com\nbob@example.com"
        };
        root.threads = [pending];
        root.selectedThreadKey = pending.key;
        composer.text = "hello";
        check(!sendMessageButton.enabled, "unconfigured group allowed a reply");
        root.reload();
        const initialRead = latestRead();

        const saved = Object.assign({}, pending, {reply_ready: true, participants_required: false});
        root.groupParticipantsBusy = true;
        backendBridge.response("set_group_participants", 1, saved);
        check(sendMessageButton.enabled, "saving members did not immediately enable replies");
        check(latestRead() !== initialRead, "save did not replace the pending history read");
        backendBridge.response("threads", initialRead, [pending]);
        check(sendMessageButton.enabled, "old read undid the first members save");
        backendBridge.response("threads", latestRead(), [saved]);
        sendMessageButton.clicked();
        check(sends().length === 1 && sends()[0].args.confirm_group === true,
          "Send did not approve the saved group");
        check(sends()[0].args.thread_key === saved.key && sends()[0].args.body === "hello"
          && sends()[0].args.expected_group_token === saved.confirmation_token,
          "reply was not bound to the displayed roster");
        check(!sendMessageButton.enabled, "duplicate send allowed");
        backendBridge.response("send_to_thread", 1, "transfer");
        check(composer.text === "", "successful reply did not clear draft");

        composer.text = "next reply";
        const oldRead = latestRead();
        const reviewed = Object.assign({}, saved, {
          recipients: ["alice@example.com", "carol@example.com"],
          confirmation_token: "\nalice@example.com\ncarol@example.com"
        });
        root.groupParticipantsBusy = true;
        check(!sendMessageButton.enabled, "reply allowed while members were being saved");
        backendBridge.response("set_group_participants", 2, reviewed);
        check(sendMessageButton.enabled, "saved members did not restore Send");
        check(root.selectedThread().recipients.join() === reviewed.recipients.join(),
          "saved members did not replace the displayed recipients immediately");
        sendMessageButton.clicked();
        check(sends()[1].args.expected_group_token === reviewed.confirmation_token,
          "immediate reply used the old roster token");

        const freshRead = latestRead();
        check(freshRead !== oldRead, "save did not request a fresh snapshot");
        const refreshed = Object.assign({}, reviewed, {starred: true});
        if (LATE_AFTER_REFRESH) backendBridge.response("threads", freshRead, [refreshed]);
        if (LATE_READ_FAILED) backendBridge.failure("threads", oldRead, "obsolete failure");
        else backendBridge.response("threads", oldRead, [saved]);
        check(root.selectedThread().confirmation_token === reviewed.confirmation_token,
          "late history replaced the saved roster");
        check(root.errorText === "", "obsolete read failure was displayed");
        check(root.threadsBusy === !LATE_AFTER_REFRESH,
          "obsolete read changed the current request's busy state");
        if (!LATE_AFTER_REFRESH) backendBridge.response("threads", freshRead, [refreshed]);
        check(root.selectedThread().starred === true, "fresh snapshot was ignored");
        backendBridge.response("send_to_thread", 3, "transfer");

        backendBridge.failure("threads", latestRead(), "current read failed");
        check(!root.threadsBusy && root.errorText === "current read failed", "current failure was ignored");
        check(root.selectedThread().confirmation_token === reviewed.confirmation_token,
          "failed refresh erased saved members");
        root.reload();
        backendBridge.response("threads", latestRead(), [reviewed]);
        check(root.errorText === "", "current read did not recover");

        const changed = Object.assign({}, reviewed, {
          reply_ready: false, participants_required: true, roster_changed: true,
          confirmation_token: "changed\nalice@example.com\ncarol@example.com"
        });
        root.threads = [changed];
        composer.text = "after roster change";
        check(!sendMessageButton.enabled, "roster needing review allowed a reply");
        const resolved = Object.assign({}, changed, {
          reply_ready: true, participants_required: false, roster_changed: false
        });
        root.groupParticipantsBusy = true;
        backendBridge.response("set_group_participants", 4, resolved);
        check(sendMessageButton.enabled, "reviewed roster required another confirmation");
        sendMessageButton.clicked();
        check(sends()[2].args.expected_group_token === resolved.confirmation_token,
          "reply reused the previous roster approval");
        backendBridge.response("send_to_thread", 5, "transfer");

        root.threads = [Object.assign({}, saved, {is_group: false, recipients: ["alice@example.com"]})];
        composer.text = "direct reply";
        check(sendMessageButton.enabled, "direct reply disabled");
        sendMessageButton.clicked();
        check(sends()[3].args.confirm_group === false && sends()[3].args.expected_group_token === "",
          "direct reply sent as group");

        const interruptedRead = latestRead();
        backendBridge.failure("", 0, "bridge restarted");
        check(!root.threadsBusy, "bridge failure retained the active read");
        backendBridge.response("threads", interruptedRead, [saved]);
        check(!root.selectedThread().is_group, "read from before restart was accepted");
        root.reload();
        const beforeReset = latestRead();
        setupController.configured = false;
        setupController.historyReset();
        backendBridge.response("threads", beforeReset, [saved]);
        backendBridge.response("threads", 0, [saved]);
        check(!root.threadsBusy && root.threads.length === 0, "old read restored cleared history");
        console.log("BLUEFERRY_GROUP_REPLY_OK");
      } catch (error) {
        console.error(error);
      }
      Qt.quit();
    }
  }
'''.replace("LATE_AFTER_REFRESH", "true" if late_after_refresh else "false").replace(
        "LATE_READ_FAILED", "true" if late_read == "failure" else "false"
    )
    config.write_text(source[:source.rfind("}")] + probe + "}\n")
    result = subprocess.run(
        [executable, "--path", str(config)], env=quickshell_environment,
        capture_output=True, text=True, timeout=10, check=False,
    )
    log = result.stdout + result.stderr
    assert result.returncode == 0 and "BLUEFERRY_GROUP_REPLY_OK" in log, log
    assert "WARN scene:" not in log and "ReferenceError" not in log and "TypeError" not in log, log


class _CallsBridge(QObject):
    """Inert recorder for RecentCallsPage; it performs no I/O."""

    def __init__(self) -> None:
        super().__init__()
        self.loads = 0
        self.syncs = 0

    @Property("QVariantMap", constant=True)
    def callHistoryRows(self):
        missed = {"direction": "missed", "caller": "<b>Eve</b>", "known": True,
                  "address": "+15551230002", "day": "Today", "clock": "2:36 PM",
                  "missed": True, "count": 3}
        outgoing = {"direction": "outgoing", "caller": "+15551230001", "known": False,
                    "address": "+15551230001", "day": "Today", "clock": "1:02 PM",
                    "missed": False, "count": 1}
        return {"all": [missed, outgoing], "missed": [missed]}

    @Property("QVariantMap", constant=True)
    def status(self):
        return {}

    @Property(int, constant=True)
    def avatarRevision(self) -> int:
        return 0

    @Property(str, constant=True)
    def callsState(self) -> str:
        return "disabled"

    @Slot(str, result=str)
    def avatarSource(self, _address: str) -> str:
        return ""

    @Property(str, constant=True)
    def callHistoryError(self) -> str:
        return ""

    @Property(bool, constant=True)
    def busy(self) -> bool:
        return False

    @Slot()
    def loadCallHistory(self) -> None:
        self.loads += 1

    @Slot()
    def syncCallHistory(self) -> None:
        self.syncs += 1


def test_recent_calls_page_loads_once_and_filters_missed_calls(qml_engine):
    from PySide6.QtQml import QQmlExpression

    bridge = _CallsBridge()
    component = _component(qml_engine, "src/blueferry/qt/qml/RecentCallsPage.qml")
    page = component.createWithInitialProperties({"bridge": bridge})
    assert page is not None, [error.toString() for error in component.errors()]

    def evaluate(script):
        expression = QQmlExpression(qml_engine.contextForObject(page), page, script)
        value, failed = expression.evaluate()
        assert not failed, expression.error().toString()
        return value

    assert bridge.loads == 0, "creating the page must not fetch call records"
    assert evaluate("visibleCalls().length") == 2
    page.setProperty("missedOnly", True)
    assert evaluate("visibleCalls().map(call => call.direction).join()") == "missed"
    assert bridge.syncs == 0
    # Remote names and numbers are rendered as plain text, never as markup.
    source = (ROOT / "src/blueferry/qt/qml/RecentCallsPage.qml").read_text()
    assert source.count("Controls.Label {") == source.count("textFormat: Text.PlainText")
    page.deleteLater()


class _OpenRuleBridge(QObject):
    """Inert recorder: the editor may only request validated backend edits."""

    notificationOpenMapChanged = Signal()
    statusChanged = Signal()
    busyChanged = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._rules: list[dict] = []
        self._revision = 7
        self.calls: list[tuple] = []

    @Property("QVariantList", notify=notificationOpenMapChanged)
    def notificationOpenMap(self):
        return self._rules

    @Property("QVariantMap", notify=statusChanged)
    def status(self):
        return {
            "daemon": True,
            "notification_policy": "all",
            "notification_open_map_revision": self._revision,
        }

    def bump(self, revision: int) -> None:
        self._revision = revision
        self.statusChanged.emit()

    @Property(bool, notify=busyChanged)
    def busy(self):
        return False

    @Slot()
    def loadNotificationOpenMap(self) -> None:
        self.calls.append(("load",))

    @Slot(str, str)
    def setNotificationOpenTarget(self, bundle_id: str, target: str) -> None:
        self.calls.append(("set", bundle_id, target))

    @Slot(str)
    def removeNotificationOpenTarget(self, bundle_id: str) -> None:
        self.calls.append(("remove", bundle_id))

    def publish(self, rules) -> None:
        self._rules = rules
        self.notificationOpenMapChanged.emit()


def test_qt_click_rule_editor_adds_lists_and_removes_rules(qml_engine) -> None:
    bridge = _OpenRuleBridge()
    component = _component(qml_engine, "src/blueferry/qt/qml/NotificationOpenMapEditor.qml")
    editor = component.createWithInitialProperties({"bridge": bridge})
    assert editor is not None, "\n".join(error.toString() for error in component.errors())
    try:
        assert bridge.calls == [("load",)]
        # Unrelated status refreshes don't reread the rules.
        bridge.statusChanged.emit()
        assert bridge.calls == [("load",)]
        # Rule edits made elsewhere (CLI) bump the revision.
        bridge.bump(8)
        assert bridge.calls == [("load",), ("load",)]
        bundle = editor.findChild(QObject, "openRuleBundleField")
        target = editor.findChild(QObject, "openRuleTargetField")
        add = editor.findChild(QObject, "addOpenRule")
        assert add.property("enabled") is False

        bundle.setProperty("text", "  net.whatsapp.WhatsApp ")
        target.setProperty("text", "https://web.whatsapp.com")
        assert add.property("enabled") is True
        QMetaObject.invokeMethod(add, "clicked")
        assert bridge.calls[-1] == ("set", "net.whatsapp.WhatsApp", "https://web.whatsapp.com")

        # The form clears only once the backend returns the accepted rule.
        assert bundle.property("text") != ""
        bridge.publish([{
            "bundle_id": "net.whatsapp.WhatsApp",
            "target": "https://web.whatsapp.com",
            "kind": "url",
        }])
        QGuiApplication.processEvents()
        assert bundle.property("text") == ""
        assert target.property("text") == ""

        def visual_children(item):
            for child in item.childItems():
                yield child
                yield from visual_children(child)

        [remove] = [
            item for item in visual_children(editor) if item.objectName() == "removeOpenRule"
        ]
        QMetaObject.invokeMethod(remove, "clicked")
        assert bridge.calls[-1] == ("remove", "net.whatsapp.WhatsApp")
    finally:
        editor.deleteLater()


def test_now_playing_bar_is_absent_until_media_is_available(
    qml_engine, settings_window,
) -> None:
    window, _bridge = settings_window
    assert window.findChild(QObject, "nowPlayingBar") is None

    _evaluate(qml_engine, """testBridge.nowPlaying = {
        enabled: true, available: true, player: {state: "playing"},
        track: {title: "<b>Title</b>", artist: "Artist"},
        supported_commands: ["next", "toggle"]
    }""")
    QGuiApplication.processEvents()
    assert window.findChild(QObject, "nowPlayingBar") is not None
    label = window.findChild(QObject, "nowPlayingSummary")
    # Remote text is shown verbatim, never as markup.
    assert label.property("text") == "<b>Title</b> — Artist"
    qml_engine.globalObject().setProperty("nowPlayingLabel", qml_engine.newQObject(label))
    assert _evaluate(qml_engine, "nowPlayingLabel.textFormat") == 0  # Text.PlainText
    assert not window.findChild(QObject, "nowPlayingPrevious").property("enabled")
    assert window.findChild(QObject, "nowPlayingBar").property("stateIcon") == (
        "media-playback-start"
    )
    _evaluate(qml_engine, """testBridge.nowPlaying = Object.assign(
        {}, testBridge.nowPlaying, {player: {state: "paused"}})""")
    QGuiApplication.processEvents()
    assert window.findChild(QObject, "nowPlayingBar").property("stateIcon") == (
        "media-playback-pause"
    )
    _evaluate(qml_engine, """testBridge.nowPlaying = Object.assign(
        {}, testBridge.nowPlaying, {player: {state: "playing"}})""")
    QGuiApplication.processEvents()

    # An unknown list (not sent after a reconnect) keeps the transport usable;
    # a known list without "previous" disables it again.
    _evaluate(qml_engine, """testBridge.nowPlaying = Object.assign(
        {}, testBridge.nowPlaying, {supported_commands: [], supported_commands_known: false})""")
    QGuiApplication.processEvents()
    assert window.findChild(QObject, "nowPlayingPrevious").property("enabled")
    _evaluate(qml_engine, """testBridge.nowPlaying = Object.assign(
        {}, testBridge.nowPlaying,
        {supported_commands: ["next", "toggle"], supported_commands_known: true})""")
    QGuiApplication.processEvents()
    assert not window.findChild(QObject, "nowPlayingPrevious").property("enabled")

    _click_control(window, window.findChild(QObject, "nowPlayingNext"))
    _click_control(window, window.findChild(QObject, "nowPlayingToggle"))
    assert _evaluate(
        qml_engine,
        "testBridge.calls.filter(c => c.method === 'sendMediaCommand').map(c => c.args[0])",
    ) == ["next", "toggle"]

    _evaluate(qml_engine, "testBridge.nowPlaying = {enabled: true, available: false}")
    from PySide6.QtCore import QCoreApplication, QEvent

    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    QGuiApplication.processEvents()
    assert window.findChild(QObject, "nowPlayingBar") is None


def test_contact_photo_opt_in_appears_only_for_supporting_daemons(qml_engine, settings_window):
    window, bridge = settings_window
    bridge.setProperty("setupLoaded", True)
    QGuiApplication.processEvents()
    loader = _settings_object(window, "contactPhotosLoader")
    bridge.setProperty("status", {"daemon": True})
    assert loader.property("active") is False

    bridge.setProperty("status", {"daemon": True, "contact_photos": False})
    QGuiApplication.processEvents()
    assert loader.property("active") is False

    bridge.setProperty("status", {"daemon": True, "contact_photos": False, "saved_opt_ins": True})
    QGuiApplication.processEvents()
    assert loader.property("active") is True
    caption = _settings_object(window, "contactPhotosCaption").property("text")
    assert "next contact sync" in caption and "erases" in caption
    checkbox = _settings_object(window, "contactPhotosCheckBox")
    assert checkbox.property("checked") is False

    assert QMetaObject.invokeMethod(checkbox, "toggle")
    assert QMetaObject.invokeMethod(checkbox, "clicked")
    assert _evaluate(
        qml_engine, "testBridge.calls.filter(c => c.method === 'setContactPhotos')"
    ) == [{"method": "setContactPhotos", "args": [True]}]

    bridge.setProperty("status", {"daemon": True, "contact_photos": True, "saved_opt_ins": True})
    QGuiApplication.processEvents()
    assert checkbox.property("checked") is True


def test_otp_autocopy_opt_in_appears_only_for_supporting_daemons(qml_engine, settings_window):
    window, bridge = settings_window
    bridge.setProperty("setupLoaded", True)
    QGuiApplication.processEvents()
    loader = _settings_object(window, "otpAutocopyLoader")
    bridge.setProperty("status", {"daemon": True})
    assert loader.property("active") is False

    bridge.setProperty("status", {"daemon": True, "otp_autocopy": False})
    QGuiApplication.processEvents()
    assert loader.property("active") is False

    bridge.setProperty("status", {"daemon": True, "otp_autocopy": False, "saved_opt_ins": True})
    QGuiApplication.processEvents()
    assert loader.property("active") is True
    caption = _settings_object(window, "otpAutocopyCaption").property("text")
    assert "not in your contacts" in caption and "group chats" in caption
    checkbox = _settings_object(window, "otpAutocopyCheckBox")
    assert checkbox.property("checked") is False

    assert QMetaObject.invokeMethod(checkbox, "toggle")
    assert QMetaObject.invokeMethod(checkbox, "clicked")
    assert _evaluate(
        qml_engine, "testBridge.calls.filter(c => c.method === 'setOtpAutocopy')"
    ) == [{"method": "setOtpAutocopy", "args": [True]}]

    bridge.setProperty("status", {"daemon": True, "otp_autocopy": True, "saved_opt_ins": True})
    QGuiApplication.processEvents()
    assert checkbox.property("checked") is True


def test_phone_calls_opt_in_appears_only_for_supporting_daemons(qml_engine, settings_window):
    window, bridge = settings_window
    bridge.setProperty("setupLoaded", True)
    QGuiApplication.processEvents()
    loader = _settings_object(window, "phoneCallsLoader")
    bridge.setProperty("status", {"daemon": True})
    assert loader.property("active") is False

    bridge.setProperty("status", {"daemon": True, "calls_enabled": False})
    QGuiApplication.processEvents()
    assert loader.property("active") is True
    notice = _settings_object(window, "phoneCallsNotice").property("text")
    assert "audio plays here" in notice
    # Ticking the box is not the whole setup; the caption points at the guide.
    assert "oFono set as the hands-free backend" in notice
    assert "BlueZ's own HFP plugin disabled" in notice
    assert "documentation" in notice
    checkbox = _settings_object(window, "phoneCallsCheckBox")
    assert checkbox.property("checked") is False

    assert QMetaObject.invokeMethod(checkbox, "toggle")
    assert QMetaObject.invokeMethod(checkbox, "clicked")
    assert _evaluate(
        qml_engine, "testBridge.calls.filter(c => c.method === 'setCallsEnabled')"
    ) == [{"method": "setCallsEnabled", "args": [True]}]

    bridge.setProperty("status", {"daemon": True, "calls_enabled": True})
    QGuiApplication.processEvents()
    assert checkbox.property("checked") is True


def test_battery_warning_checkbox_follows_the_daemon(qml_engine, settings_window):
    window, bridge = settings_window
    bridge.setProperty("setupLoaded", True)
    QGuiApplication.processEvents()
    checkbox = _settings_object(window, "phoneBatteryWarningCheckBox")
    bridge.setProperty("status", {"daemon": True})
    QGuiApplication.processEvents()
    assert checkbox.property("visible") is False

    bridge.setProperty("status", {"daemon": True, "phone_battery_warning": False})
    QGuiApplication.processEvents()
    assert checkbox.property("checked") is False
    assert QMetaObject.invokeMethod(checkbox, "toggle")
    assert QMetaObject.invokeMethod(checkbox, "clicked")
    assert _evaluate(
        qml_engine, "testBridge.calls.filter(c => c.method === 'setPhoneBatteryWarning')"
    ) == [{"method": "setPhoneBatteryWarning", "args": [True]}]


def test_quickshell_sound_switch_follows_the_route_and_sends_it(qml_engine, quickshell_setup):
    base = {"notification_policy": "all", "contacts_only_notifications": False}
    page, calls, close = _quickshell_settings_page(qml_engine, quickshell_setup, base)
    switch = page.findChild(QObject, "phoneAudioCheckBox")
    hint = page.findChild(QObject, "phoneAudioHint")
    assert switch is not None and hint is not None
    assert switch.property("visible") is False and hint.property("visible") is False

    page.setProperty("status", {
        **base, "phone_audio_route": "unavailable",
        "phone_audio_view": {"available": False, "hint": "Available while the iPhone is connected."},
    })
    QGuiApplication.processEvents()
    assert switch.property("visible") is True and switch.property("enabled") is False
    assert hint.property("text") == "Available while the iPhone is connected."

    page.setProperty("status", {
        **base, "phone_audio_route": "phone",
        "phone_audio_view": {"available": True, "hint": "Playing on the iPhone."},
    })
    QGuiApplication.processEvents()
    assert switch.property("checked") is False and switch.property("enabled") is True
    page.setProperty("busy", {"phoneAudio": True})
    QGuiApplication.processEvents()
    assert switch.property("enabled") is False
    page.setProperty("busy", {})
    QGuiApplication.processEvents()
    assert QMetaObject.invokeMethod(switch, "toggle")
    assert QMetaObject.invokeMethod(switch, "clicked")
    assert calls == [("set_phone_audio_route", {"route": "pc"})]
    close()


def test_auto_profile_reset_checkbox_follows_the_daemon(qml_engine, settings_window):
    window, bridge = settings_window
    bridge.setProperty("setupLoaded", True)
    QGuiApplication.processEvents()
    checkbox = _settings_object(window, "autoProfileResetCheckBox")
    bridge.setProperty("status", {"daemon": True})
    QGuiApplication.processEvents()
    assert checkbox.property("visible") is False

    bridge.setProperty("status", {"daemon": True, "profile_reset_auto": True})
    QGuiApplication.processEvents()
    assert checkbox.property("checked") is True
    assert QMetaObject.invokeMethod(checkbox, "toggle")
    assert QMetaObject.invokeMethod(checkbox, "clicked")
    assert _evaluate(
        qml_engine, "testBridge.calls.filter(c => c.method === 'setAutoProfileReset')"
    ) == [{"method": "setAutoProfileReset", "args": [False]}]


def test_quickshell_auto_profile_reset_checkbox_is_shown_when_reported(
    qml_engine, quickshell_setup,
):
    base = {"notification_policy": "all", "contacts_only_notifications": False}
    page, calls, close = _quickshell_settings_page(qml_engine, quickshell_setup, base)
    checkbox = page.findChild(QObject, "autoProfileResetCheckBox")
    assert checkbox is not None
    assert checkbox.property("visible") is False

    page.setProperty("status", {**base, "profile_reset_auto": True})
    QGuiApplication.processEvents()
    assert checkbox.property("visible") is True
    assert checkbox.property("checked") is True
    page.setProperty("busy", {"autoProfileReset": True})
    QGuiApplication.processEvents()
    assert checkbox.property("enabled") is False
    page.setProperty("busy", {})
    QGuiApplication.processEvents()
    assert QMetaObject.invokeMethod(checkbox, "toggle")
    assert QMetaObject.invokeMethod(checkbox, "clicked")
    assert calls == [("set_auto_profile_reset", {"enabled": False})]
    close()


def test_quickshell_media_checkboxes_are_opt_in_and_the_player_needs_media_control(
    qml_engine, quickshell_setup,
):
    base = {"notification_policy": "all", "contacts_only_notifications": False}
    page, calls, close = _quickshell_settings_page(qml_engine, quickshell_setup, base)
    media = page.findChild(QObject, "mediaControlCheckBox")
    mpris = page.findChild(QObject, "mprisPlayerCheckBox")
    warning = page.findChild(QObject, "mprisPlayerWarning")
    assert media is not None and mpris is not None and warning is not None
    # Daemons that do not report the keys do not support the settings.
    assert media.property("visible") is False
    assert mpris.property("visible") is False
    assert warning.property("visible") is False

    # A daemon with media control but without the MPRIS player.
    page.setProperty("status", {**base, "media_control_enabled": False})
    QGuiApplication.processEvents()
    assert media.property("visible") is True
    assert media.property("checked") is False
    assert media.property("enabled") is True
    assert mpris.property("visible") is False

    status = {**base, "media_control_enabled": False, "media_mpris_enabled": False}
    page.setProperty("status", status)
    QGuiApplication.processEvents()
    assert mpris.property("visible") is True
    assert mpris.property("enabled") is False
    assert "every application" in warning.property("text")

    page.setProperty("busy", {"mediaControl": True})
    QGuiApplication.processEvents()
    assert media.property("enabled") is False
    page.setProperty("busy", {})
    assert QMetaObject.invokeMethod(media, "toggle")
    assert QMetaObject.invokeMethod(media, "clicked")
    assert calls == [("set_media_control", {"enabled": True})]

    page.setProperty("status", {**status, "media_control_enabled": True})
    QGuiApplication.processEvents()
    assert media.property("checked") is True
    assert mpris.property("enabled") is True
    assert mpris.property("checked") is False
    page.setProperty("busy", {"mprisPlayer": True})
    QGuiApplication.processEvents()
    assert mpris.property("enabled") is False
    page.setProperty("busy", {})
    assert QMetaObject.invokeMethod(mpris, "toggle")
    assert QMetaObject.invokeMethod(mpris, "clicked")
    assert calls[-1] == ("set_mpris_player", {"enabled": True})
    close()


def test_quickshell_call_history_checkboxes_are_opt_in(qml_engine, quickshell_setup):
    base = {"notification_policy": "all", "contacts_only_notifications": False}
    page, calls, close = _quickshell_settings_page(qml_engine, quickshell_setup, base)
    history = page.findChild(QObject, "callHistoryCheckBox")
    popups = page.findChild(QObject, "missedCallPopupsCheckBox")
    note = page.findChild(QObject, "callHistoryNote")
    assert history is not None and popups is not None and note is not None
    # Daemons that do not report the keys do not support the settings.
    for item in (history, popups, note):
        assert item.property("visible") is False

    status = {**base, "call_history_enabled": False, "missed_call_notifications": True}
    page.setProperty("status", status)
    QGuiApplication.processEvents()
    assert history.property("visible") is True
    assert history.property("checked") is False
    assert history.property("enabled") is True
    assert popups.property("checked") is True
    # Missed-call popups are only offered while call history is on.
    assert popups.property("enabled") is False
    assert "erases the retained calls" in note.property("text")

    page.setProperty("busy", {"callHistory": True})
    QGuiApplication.processEvents()
    assert history.property("enabled") is False
    page.setProperty("busy", {})
    assert QMetaObject.invokeMethod(history, "toggle")
    assert QMetaObject.invokeMethod(history, "clicked")
    assert calls == [
        ("set_call_history", {"enabled": True, "missed_call_notifications": True}),
    ]

    page.setProperty("status", {**status, "call_history_enabled": True})
    QGuiApplication.processEvents()
    assert history.property("checked") is True
    assert popups.property("enabled") is True
    assert QMetaObject.invokeMethod(popups, "toggle")
    assert QMetaObject.invokeMethod(popups, "clicked")
    assert calls[-1] == (
        "set_call_history", {"enabled": True, "missed_call_notifications": False},
    )
    close()


@pytest.fixture
def quickshell_photos(qml_engine):
    component = QQmlComponent(qml_engine)
    component.setData(b'''import QtQuick
QtObject {
  property var calls: []
  property int nextId: 1
  function request(method, args) {
    const id = nextId++;
    calls.push({id: id, method: method, args: args});
    return id;
  }
}''', QUrl())
    bridge = component.create()
    assert bridge is not None
    cache_component = _component(qml_engine, "data/quickshell/AvatarCache.qml")
    cache = cache_component.createWithInitialProperties({
        "bridge": bridge,
    })
    assert cache is not None
    qml_engine.globalObject().setProperty("testPhotoCache", qml_engine.newQObject(cache))
    qml_engine.globalObject().setProperty("testPhotoBridge", qml_engine.newQObject(bridge))
    yield cache, bridge
    cache.deleteLater()
    bridge.deleteLater()
    QGuiApplication.processEvents()


def _avatar_data_url():
    image = QImage(32, 16, QImage.Format.Format_RGB32)
    image.fill(0x3366CC)
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    return "data:image/png;base64," + base64.b64encode(bytes(data)).decode("ascii")


def test_quickshell_avatar_cache_is_opt_in_and_coalesces_reads(qml_engine, quickshell_photos):
    cache, _bridge = quickshell_photos
    assert _evaluate(qml_engine, 'testPhotoCache.source("alice@example.com")') == ""
    assert _evaluate(qml_engine, "testPhotoBridge.calls.length") == 0
    cache.setProperty("backendStatus", {"contact_photos": True, "contact_photo_revision": 1})
    _evaluate(qml_engine, 'testPhotoCache.source("alice@example.com")')
    _evaluate(qml_engine, 'testPhotoCache.source("alice@example.com")')
    assert _evaluate(qml_engine, "testPhotoBridge.calls.length") == 1
    source = _avatar_data_url()
    result = json.dumps({"address": "alice@example.com", "source": source})
    _evaluate(qml_engine, f"testPhotoCache.accept(1, {result})")
    assert _evaluate(qml_engine, 'testPhotoCache.source("alice@example.com")') == source
    assert _evaluate(qml_engine, "testPhotoBridge.calls.length") == 1


@pytest.mark.parametrize("next_status", [
    {"contact_photos": False},
    {"contact_photos": True, "contact_photo_revision": 2},
    {},
])
def test_quickshell_ignores_late_photos_after_refresh_or_disable(
    qml_engine, quickshell_photos, next_status,
):
    cache, _bridge = quickshell_photos
    cache.setProperty("backendStatus", {"contact_photos": True, "contact_photo_revision": 1})
    _evaluate(qml_engine, 'testPhotoCache.source("alice@example.com")')
    cache.setProperty("backendStatus", next_status)
    result = json.dumps({"address": "alice@example.com", "source": _avatar_data_url()})
    _evaluate(qml_engine, f"testPhotoCache.accept(1, {result})")
    assert _evaluate(qml_engine, 'testPhotoCache.source("alice@example.com")') == ""
    assert _evaluate(qml_engine, "testPhotoBridge.calls.length") == (
        2 if next_status.get("contact_photos") else 1
    )


def test_quickshell_missing_photos_and_failures_do_not_flood_the_bridge(qml_engine, quickshell_photos):
    cache, _bridge = quickshell_photos
    cache.setProperty("backendStatus", {"contact_photos": True})
    _evaluate(qml_engine, '''
      testPhotoCache.source("missing@example.com");
      testPhotoCache.accept(1, {address: "missing@example.com", source: ""});
      testPhotoCache.source("missing@example.com");
      testPhotoCache.source("busy@example.com");
      testPhotoCache.failed(2);
      testPhotoCache.source("busy@example.com");
    ''')
    assert _evaluate(qml_engine, "testPhotoBridge.calls.length") == 2
    _evaluate(qml_engine, '''
      testPhotoCache.retryAt["busy@example.com"] = Date.now() - 1;
      testPhotoCache.source("busy@example.com");
    ''')
    assert _evaluate(qml_engine, "testPhotoBridge.calls.length") == 3


def test_quickshell_avatar_reads_remain_bounded_across_generations(qml_engine, quickshell_photos):
    cache, _bridge = quickshell_photos
    cache.setProperty("maxPending", 2)
    for revision in range(5):
        cache.setProperty("backendStatus", {"contact_photos": True, "contact_photo_revision": revision})
        _evaluate(qml_engine, '''
          testPhotoCache.source("one@example.com");
          testPhotoCache.source("two@example.com");
          testPhotoCache.source("three@example.com");
        ''')
    assert _evaluate(qml_engine, "testPhotoBridge.calls.length") == 2
    _evaluate(qml_engine, 'testPhotoCache.accept(1, {address: "one@example.com", source: ""})')
    _evaluate(qml_engine, 'testPhotoCache.source("three@example.com")')
    assert _evaluate(qml_engine, "testPhotoBridge.calls.length") == 3
    _evaluate(qml_engine, "testPhotoCache.disconnected()")
    assert _evaluate(qml_engine, "Object.keys(testPhotoCache.tickets).length") == 0


def test_quickshell_full_avatar_cache_does_not_thrash_visible_images(qml_engine, quickshell_photos):
    cache, _bridge = quickshell_photos
    cache.setProperty("backendStatus", {"contact_photos": True})
    cache.setProperty("maxChars", 1)
    cache.setProperty("maxEntries", 2)
    source = json.dumps(_avatar_data_url())
    _evaluate(qml_engine, f'''
      testPhotoCache.source("one@example.com");
      testPhotoCache.accept(1, {{address: "one@example.com", source: {source}}});
      testPhotoCache.source("two@example.com");
      testPhotoCache.accept(2, {{address: "two@example.com", source: {source}}});
      for (let i = 0; i < 5; ++i) {{
        testPhotoCache.source("one@example.com");
        testPhotoCache.source("two@example.com");
        testPhotoCache.source("three@example.com");
      }}
    ''')
    assert _evaluate(qml_engine, "testPhotoBridge.calls.length") == 2
    assert cache.property("cachedChars") == 0


def test_quickshell_avatar_renders_a_photo_and_preserves_group_fallback(qml_engine, quickshell_photos):
    cache, _bridge = quickshell_photos
    theme_component = _component(qml_engine, "data/quickshell/ThemePalette.qml")
    theme = theme_component.create()
    component = _component(qml_engine, "data/quickshell/ContactAvatar.qml")
    avatar = component.createWithInitialProperties({
        "photos": cache, "ferryTheme": theme,
        "thread": {"name": "Alice", "is_group": False, "recipients": ["alice@example.com"]},
    })
    assert avatar is not None
    cache.setProperty("backendStatus", {"contact_photos": True})
    QGuiApplication.processEvents()
    assert _evaluate(qml_engine, "testPhotoBridge.calls.length") == 1
    result = json.dumps({"address": "alice@example.com", "source": _avatar_data_url()})
    _evaluate(qml_engine, f"testPhotoCache.accept(1, {result})")
    assert avatar.property("photoSource") == _avatar_data_url()
    for _ in range(100):
        if avatar.property("photoReady"):
            break
        QTest.qWait(10)
    assert avatar.property("photoReady") is True
    avatar.setProperty("thread", {
        "name": "Service", "is_group": False, "reply_ready": False,
        "recipients": ["alice@example.com"],
    })
    QGuiApplication.processEvents()
    assert avatar.property("photoSource") == ""
    avatar.setProperty("thread", {
        "name": "Crew", "is_group": True, "recipients": ["alice@example.com"],
    })
    QGuiApplication.processEvents()
    assert avatar.property("photoReady") is False
    assert avatar.property("photoSource") == ""
    assert _evaluate(qml_engine, "testPhotoBridge.calls.length") == 1
    avatar.deleteLater()
    theme.deleteLater()


@pytest.mark.private_dbus
def test_quickshell_shell_shares_photos_and_discards_late_opt_out_results(
    tmp_path, quickshell_environment,
):
    import shutil
    import subprocess

    executable = shutil.which("quickshell")
    if executable is None:
        pytest.skip("Quickshell is not installed")
    for source in (ROOT / "data/quickshell").glob("*.qml"):
        shutil.copyfile(source, tmp_path / source.name)
    shutil.copyfile(
        ROOT / "src/blueferry/qt/qml/ConversationLogic.qml", tmp_path / "ConversationLogic.qml",
    )
    (tmp_path / "Theme.qml").write_text("import QtQuick\nThemePalette {}\n")
    (tmp_path / "SetupTransport.qml").write_text('''import QtQuick
Item {
  signal lineReceived(int id, string kind, string line)
  signal finished(int id, string kind, int code, string output, string diagnostic)
  function execute(id, kind, command, interactive) {}
  function cancel(id) {}
  function write(id, text) {}
}
''')
    (tmp_path / "BackendBridge.qml").write_text('''import QtQuick
Item {
  property bool desktopClient: false
  property var calls: []
  property int nextId: 1
  signal response(string method, int requestId, var result)
  signal failure(string method, int requestId, string message)
  signal eventReceived(string name, var data)
  function request(method, args) {
    const id = nextId++;
    calls.push({id: id, method: method, args: args});
    return id;
  }
  function requestLatest(method, args) {}
  function cancelLatest(method) {}
}
''')
    config = tmp_path / "shell.qml"
    source = config.read_text()
    probe = r'''
  Timer {
    interval: 100; repeat: true; running: true
    property int phase: 0
    property int attempts: 0
    onTriggered: {
      function check(value, message) { if (!value) throw new Error(message); }
      function reads() { return backendBridge.calls.filter(call => call.method === "contact_photo"); }
      function status(enabled, revision) {
        backendBridge.response("status", 1, {
          daemon: true, contact_photos: enabled, contact_photo_revision: revision,
          storage_policy: "plaintext", storage_state: "ready"
        });
      }
      try {
        check(attempts++ < 30, "avatar probe did not finish");
        if (phase === 0) {
          setupController.configured = true;
          root.threads = [
            {key: "alice", name: "Alice", recipients: ["alice@example.com"],
             is_group: false, messages: [], reply_ready: true},
            {key: "crew", name: "Crew", recipients: ["alice@example.com"],
             is_group: true, messages: [], reply_ready: true}
          ];
          root.selectedThreadKey = "alice";
          status(true, 1);
          phase = 1;
        } else if (phase === 1) {
          check(reads().length === 1, "list and header did not share a read or group fetched a photo");
          backendBridge.response("contact_photo", reads()[0].id,
            {address: "alice@example.com", source: PHOTO_SOURCE});
          phase = 2;
        } else if (phase === 2) {
          if (!conversationAvatar.photoReady) return;
          root.selectedThreadKey = "crew";
          phase = 3;
        } else if (phase === 3) {
          check(!conversationAvatar.photoReady, "group displayed a participant photo");
          check(reads().length === 1, "group caused an additional photo read");
          root.selectedThreadKey = "alice";
          status(true, 2);
          phase = 4;
        } else if (phase === 4) {
          check(reads().length === 2, "contact refresh did not refetch the photo");
          status(false, 2);
          backendBridge.response("contact_photo", reads()[1].id,
            {address: "alice@example.com", source: PHOTO_SOURCE});
          phase = 5;
        } else {
          check(!conversationAvatar.photoReady, "late reply restored an opted-out photo");
          check(reads().length === 2, "opt-out caused another photo read");
          console.log("BLUEFERRY_AVATARS_OK");
          Qt.quit();
        }
      } catch (error) {
        console.error(error);
        Qt.quit();
      }
    }
  }
'''.replace("PHOTO_SOURCE", json.dumps(_avatar_data_url()))
    config.write_text(source[:source.rfind("}")] + probe + "}\n")
    result = subprocess.run(
        [executable, "--path", str(config)], env=quickshell_environment,
        capture_output=True, text=True, timeout=10, check=False,
    )
    log = result.stdout + result.stderr
    assert result.returncode == 0 and "BLUEFERRY_AVATARS_OK" in log, log
    assert "WARN scene:" not in log and "ReferenceError" not in log and "TypeError" not in log, log


def _open_category(qml_engine, window, category):
    _evaluate(qml_engine, "testWindow.openPhoneSettings()")
    QGuiApplication.processEvents()
    page = window.findChild(QObject, "phoneSettingsPage")
    QMetaObject.invokeMethod(page, "open", Q_ARG("QVariant", category))
    QGuiApplication.processEvents()
    return page


def test_settings_categories_switch_features_and_ask_for_a_restart(qml_engine, settings_window):
    window, bridge = settings_window
    bridge.setProperty("status", {"daemon": True})
    pinned_item = {
        "value": True, "running": False, "source": "environment",
        "variable": "BLUEFERRY_SHOW_NOTIFICATION_CONTENT", "restart_required": False,
    }
    bridge.setProperty("features", {"loaded": True, "available": True, "items": {
        "mark_read_on_dismiss": {
            "value": False, "running": False, "source": "default",
            "variable": "BLUEFERRY_MARK_READ_ON_DISMISS", "restart_required": False,
        },
        "show_notification_content": pinned_item,
    }})
    _open_category(qml_engine, window, "notifications")
    assert bridge.property("featureLoads") >= 1
    switch = _settings_object(window, "feature_mark_read_on_dismiss")
    assert switch.property("enabled") is True and switch.property("checked") is False
    QMetaObject.invokeMethod(switch, "toggle")
    QMetaObject.invokeMethod(switch, "toggled")
    assert _evaluate(qml_engine, "testBridge.calls")[-1] == {
        "method": "setFeature", "args": ["mark_read_on_dismiss", True],
    }
    # One the service environment sets cannot be changed here.
    pinned = _settings_object(window, "feature_show_notification_content")
    assert pinned.property("enabled") is False and "environment" in pinned.property("subtitle")
    notice = _settings_object(window, "restartNotice")
    assert notice.property("visible") is False
    # A switch the daemon does not report (older daemon) names the variable.
    bridge.setProperty("features", {"loaded": True, "available": True, "items": {
        "show_notification_content": pinned_item,
    }})
    QGuiApplication.processEvents()
    assert switch.property("enabled") is False
    assert "BLUEFERRY_MARK_READ_ON_DISMISS" in switch.property("subtitle")
    bridge.setProperty("features", {"loaded": True, "available": True, "items": {
        "mark_read_on_dismiss": {
            "value": True, "running": False, "source": "settings",
            "variable": "BLUEFERRY_MARK_READ_ON_DISMISS", "restart_required": True,
        },
    }})
    QGuiApplication.processEvents()
    assert notice.property("visible") is True
    _evaluate(qml_engine, "testWindow.closePhoneSettings()")


def test_settings_drill_down_in_a_narrow_window(qml_engine, settings_window):
    window, _bridge = settings_window
    page = _open_category(qml_engine, window, "phone")
    page.setProperty("drilled", False)
    window.resize(440, window.height())
    QGuiApplication.processEvents()
    assert page.property("narrow") is True
    categories = window.findChild(QObject, "settingsCategories")
    detail = window.findChild(QObject, "settingsDetail")
    assert categories.property("visible") is True and detail.property("visible") is False
    QMetaObject.invokeMethod(page, "open", Q_ARG("QVariant", "about"))
    QGuiApplication.processEvents()
    assert categories.property("visible") is False and detail.property("visible") is True
    assert _settings_object(window, "aboutButton") is not None
    assert _settings_object(window, "shortcutsButton") is not None
    _evaluate(qml_engine, "testWindow.closePhoneSettings()")


def test_settings_run_the_doctor_and_show_its_report_as_plain_text(qml_engine, settings_window):
    window, bridge = settings_window
    bridge.setProperty("status", {"daemon": True})
    _open_category(qml_engine, window, "phone")
    button = _settings_object(window, "runDoctorButton")
    summary = _settings_object(window, "doctorSummary")
    report = _settings_object(window, "doctorReport")
    assert summary.property("visible") is False and report.property("visible") is False
    QMetaObject.invokeMethod(button, "clicked")
    assert _evaluate(qml_engine, "testBridge.calls")[-1] == {"method": "runDoctor", "args": []}
    bridge.setProperty("doctor", {"running": False, "ran": True, "ok": True, "warnings": True,
                                  "text": "WARNING doctor: <b>Adapter</b> CoD"})
    QGuiApplication.processEvents()
    assert summary.property("visible") is True
    assert "warnings" in summary.property("text")
    qml_engine.globalObject().setProperty("doctorReport", qml_engine.newQObject(report))
    assert _evaluate(qml_engine, "Number(doctorReport.textFormat)") == 0  # PlainText
    assert report.property("text") == "WARNING doctor: <b>Adapter</b> CoD"
    _evaluate(qml_engine, "testWindow.closePhoneSettings()")


def test_calls_tab_audio_switch_explains_where_the_sound_plays(qml_engine, settings_window):
    window, bridge = settings_window
    _evaluate(qml_engine, "testWindow.currentTab = testWindow.callsTab")
    bridge.setProperty("status", {"calls_enabled": True})
    bridge.setProperty("callsState", "ready")
    bridge.setProperty("phoneCalls", [
        {"call_id": "voicecall01", "state": "active", "ringing": False,
         "display_peer": "Alice", "number": "+41790000001", "first_seen": ""},
    ])
    _evaluate(qml_engine, """testBridge.phoneAudio = {
        supported: true, available: true, onPc: true, pending: false,
        playing: "phone", mismatch: true,
        hint: "The iPhone is playing on its own speaker (chosen on the iPhone)."
    }""")
    QGuiApplication.processEvents()
    switch = _visual_find(window.contentItem(), "callsAudioSwitch", qml_engine)
    assert switch is not None
    assert switch.property("hint") == (
        "The iPhone is playing on its own speaker (chosen on the iPhone)."
    )
    bridge.setProperty("phoneCalls", [])
    QGuiApplication.processEvents()


def test_calls_tab_watches_call_history_only_while_shown(qml_engine, settings_window):
    window, bridge = settings_window
    _evaluate(qml_engine, "testWindow.openRecentCalls()")
    QGuiApplication.processEvents()
    assert _evaluate(qml_engine, "testWindow.currentTab === testWindow.callsTab") is True
    assert _evaluate(qml_engine, "testBridge.calls") == [], "disabled feature: nothing loads"
    assert _settings_object(window, "callHistoryHint").property("visible") is True

    bridge.setProperty("callHistoryEnabled", True)
    bridge.setProperty("status", {"call_history_enabled": True})
    QGuiApplication.processEvents()
    assert _evaluate(qml_engine, "testBridge.calls") == [
        {"method": "watchCallHistory", "args": [True]},
    ]
    assert window.findChild(QObject, "recentCallsPage") is not None

    _evaluate(qml_engine, "testWindow.currentTab = testWindow.messagesTab")
    QGuiApplication.processEvents()
    assert _evaluate(qml_engine, "testBridge.calls")[-1] == {
        "method": "watchCallHistory", "args": [False],
    }
    assert _evaluate(qml_engine, "testWindow.pageStack.depth") == 1


def test_conversation_list_shows_one_preview_line_and_chat_fills_its_pane(
    qml_engine, settings_window
):
    window, bridge = settings_window
    window.resize(1500, 700)
    body = "DPD  Paketbestätigung\n\nGegen 9.33 Uhr\n\nbin ich angekommen."
    bridge.setProperty("threads", [{
        "key": "dpd", "name": "+212786524411", "recipients": ["+212786524411"],
        "is_group": False, "unread": False, "starred": False, "reply_ready": True,
        "messages": [{"handle": "1", "body": body, "outgoing": False,
                      "display_timestamp": "Today 09:41"}],
    }])
    window.setProperty("selectedThreadKey", "dpd")
    QTest.qWait(50)

    def value(child, expression):
        return _delegate_value(qml_engine, window, "threadList", child, expression)

    assert value("threadSubtitle", "item.text") == (
        "DPD Paketbestätigung Gegen 9.33 Uhr bin ich angekommen."
    )
    assert value("threadSubtitle", "item.lineCount") == 1
    assert value("threadMeta", "item.text") == "Today 09:41"
    # Two text lines plus padding, never the whole message.
    lines = value("threadTitle", "item.height") + value("threadSubtitle", "item.height")
    assert value("threadDelegate", "delegate.height") < lines * 2

    pane = _settings_object(window, "chatPane")
    for name in ("chatHeader", "messageList", "messageComposer", "sendButton"):
        assert pane.isAncestorOf(_settings_object(window, name)), name
    header = _settings_object(window, "chatHeader")
    assert header.mapToItem(pane, QPointF(0, 0)).y() == 0
    assert _settings_object(window, "messageComposer").width() > pane.width() * 0.7
    assert _settings_object(window, "messageList").height() > pane.height() * 0.5


def test_dial_pad_dials_only_dialable_characters(qml_engine, settings_window):
    window, bridge = settings_window
    _evaluate(qml_engine, "testBridge.featureHints = {calls: 'Set BLUEFERRY_CALLS_ENABLED=true'}")
    _evaluate(qml_engine, "testWindow.currentTab = testWindow.callsTab")
    QGuiApplication.processEvents()
    call_button = _settings_object(window, "dialPadCallButton")
    assert call_button.property("enabled") is False
    assert "BLUEFERRY_CALLS_ENABLED=true" in _settings_object(window, "callsHint").property("text")

    bridge.setProperty("status", {"calls_enabled": True})
    bridge.setProperty("callsState", "ready")
    QGuiApplication.processEvents()
    field = _settings_object(window, "dialPadField")
    qml_engine.globalObject().setProperty("callsTabItem", qml_engine.newQObject(
        _settings_object(window, "callsTab")))
    for key in ("0", "7", "9", "#", "x"):
        _evaluate(qml_engine, f"callsTabItem.append('{key}')")
    zero = _visual_find(window.contentItem(), "dialKey0", qml_engine)
    QMetaObject.invokeMethod(zero, "pressAndHold")
    QMetaObject.invokeMethod(zero, "clicked")  # the release after a long press
    assert field.property("text") == "079#+"
    assert _evaluate(qml_engine, "callsTabItem.dialable('+41 (79) 1-2<b>')") == "+417912"
    QMetaObject.invokeMethod(call_button, "clicked")
    assert _evaluate(qml_engine, "testBridge.calls")[-1] == {"method": "dialCall", "args": ["079#+"]}



def test_dial_suggestions_show_and_dial_contact_numbers_unchanged(qml_engine, settings_window):
    window, bridge = settings_window
    _evaluate(qml_engine, "testWindow.currentTab = testWindow.callsTab")
    bridge.setProperty("status", {"calls_enabled": True})
    bridge.setProperty("callsState", "ready")
    QGuiApplication.processEvents()
    _settings_object(window, "dialPadField").setProperty("text", "Kim")
    bridge.setProperty("contactResults", [
        {"name": "Pat US", "address": "+15550100123"},
        {"name": "Kim AU", "address": "0491570156"},
        {"name": "Mail", "address": "kim@example.org"},
    ])
    QGuiApplication.processEvents()
    suggestions = _settings_object(window, "dialSuggestions")
    suggestions.setProperty("width", 300)
    QMetaObject.invokeMethod(suggestions, "forceLayout")
    QGuiApplication.processEvents()
    rows = {
        str(item.property("subtitle")): item
        for item in suggestions.property("contentItem").childItems()
        if item.metaObject().indexOfSignal("clicked()") >= 0
    }
    # Shown exactly as stored: "+" only where the iPhone has one.
    assert set(rows) == {"+15550100123", "0491570156"}
    QMetaObject.invokeMethod(rows["0491570156"], "clicked")
    assert _settings_object(window, "dialPadField").property("text") == "0491570156"

def test_notifications_tab_loads_while_shown_and_renders_plain_text(qml_engine, settings_window):
    window, _bridge = settings_window
    _evaluate(qml_engine, "testWindow.currentTab = testWindow.notificationsTab")
    QGuiApplication.processEvents()
    assert _evaluate(qml_engine, "testBridge.calls") == [
        {"method": "watchNotifications", "args": [True]},
    ]
    _evaluate(qml_engine, """testBridge.featureHints = {
        notifications: "Set BLUEFERRY_NOTIFICATION_HISTORY=true"
    }""")
    QGuiApplication.processEvents()
    placeholder = _settings_object(window, "notificationsPlaceholder")
    assert "NOTIFICATION_HISTORY" in placeholder.property("explanation")

    _evaluate(qml_engine, """testBridge.featureHints = {}""")
    _evaluate(qml_engine, "testBridge.notificationsInfo = {enabled: true, content: false}")
    _evaluate(qml_engine, """testBridge.notifications = [
        {app: "<b>Chat</b>", time: "10:00 AM", title: "", subtitle: "", body: ""}
    ]""")
    QGuiApplication.processEvents()
    assert _settings_object(window, "notificationContentHint").property("visible") is True
    source = (ROOT / "src/blueferry/qt/qml/NotificationsTab.qml").read_text()
    assert source.count("Controls.Label {") == source.count("textFormat: Text.PlainText")

    _evaluate(qml_engine, "testWindow.currentTab = testWindow.messagesTab")
    QGuiApplication.processEvents()
    assert _evaluate(qml_engine, "testBridge.calls")[-1] == {
        "method": "watchNotifications", "args": [False],
    }


def test_phone_card_greys_out_opt_ins_and_switches_audio_explicitly(qml_engine, settings_window):
    window, bridge = settings_window
    card = _settings_object(window, "phoneCard")
    assert card.property("visible") is True
    assert _settings_object(window, "phoneCardName").property("text") == "Test iPhone"
    _evaluate(qml_engine, """testBridge.featureHints = {
        media: "Set BLUEFERRY_MEDIA_CONTROL_ENABLED=true", phoneStatus: "Battery <b>needs</b> calls"
    }""")
    _evaluate(qml_engine, """testBridge.phoneAudio = {
        supported: true, available: false, onPc: false, pending: false,
        hint: "Set BLUEFERRY_KEEP_PHONE_AUDIO_ON_PHONE=false"
    }""")
    QGuiApplication.processEvents()
    audio = _settings_object(window, "phoneCardAudioSwitch")
    assert audio.property("enabled") is False
    assert "KEEP_PHONE_AUDIO" in audio.property("subtitle")
    assert "MEDIA_CONTROL" in _settings_object(window, "nowPlayingHint").property("text")
    hint = _settings_object(window, "phoneStatusHint")
    qml_engine.globalObject().setProperty("phoneStatusHint", qml_engine.newQObject(hint))
    assert _evaluate(qml_engine, "phoneStatusHint.textFormat") == 0  # Text.PlainText
    assert _settings_object(window, "proximitySwitch").property("enabled") is False

    bridge.setProperty("status", {"daemon": True, "proximity_lock": "idle"})
    _evaluate(qml_engine, """testBridge.phoneAudio = {
        supported: true, available: true, onPc: false, pending: false, hint: "On the iPhone"
    }""")
    QGuiApplication.processEvents()
    _click_control(window, audio)
    assert _evaluate(qml_engine, "testBridge.calls")[-1] == {
        "method": "setPhoneAudioRoute", "args": ["pc"],
    }
    assert audio.property("checked") is False  # until the daemon reports "pc"
    _evaluate(qml_engine, """testBridge.phoneAudio = {
        supported: true, available: true, onPc: true, pending: false,
        playing: "phone", mismatch: true,
        hint: "The iPhone is playing on its own speaker (chosen on the iPhone)."
    }""")
    QGuiApplication.processEvents()
    assert audio.property("checked") is True
    assert "its own speaker" in audio.property("subtitle")
    _click_control(window, _settings_object(window, "proximitySwitch"))
    assert _evaluate(qml_engine, "testBridge.calls")[-1] == {
        "method": "setProximityLock", "args": [True, 60],
    }


def test_running_calls_show_as_cards_with_answer_hold_and_hang_up(qml_engine, settings_window):
    window, bridge = settings_window
    _evaluate(qml_engine, "testWindow.currentTab = testWindow.callsTab")
    QGuiApplication.processEvents()
    assert _visual_find(window.contentItem(), "activeCallCard", qml_engine) is None
    bridge.setProperty("status", {"calls_enabled": True})
    bridge.setProperty("callsState", "ready")
    bridge.setProperty("phoneCalls", [
        {"call_id": "voicecall01", "state": "incoming", "ringing": True,
         "display_peer": "<b>Alice</b>", "number": "+41790000001", "first_seen": ""},
    ])
    QGuiApplication.processEvents()
    card = _visual_find(window.contentItem(), "activeCallCard", qml_engine)
    assert card is not None
    assert _visual_find(card, "activeCallState").property("text") == "Incoming call…"
    hangup = _visual_find(card, "activeCallHangup")
    assert hangup.property("text") == "Decline"
    QMetaObject.invokeMethod(hangup, "clicked")
    assert _evaluate(qml_engine, "testBridge.calls")[-1] == {
        "method": "hangupCall", "args": ["voicecall01"],
    }
    bridge.setProperty("phoneCalls", [])
    QGuiApplication.processEvents()
    assert _visual_find(window.contentItem(), "activeCallCard", qml_engine) is None


def test_settings_page_keeps_the_former_menu_entries(qml_engine, settings_window):
    window, _bridge = settings_window
    _evaluate(qml_engine, "testWindow.openPhoneSettings()")
    QGuiApplication.processEvents()
    assert _settings_object(window, "aboutButton") is not None
    assert _settings_object(window, "shortcutsButton") is not None
    _evaluate(qml_engine, "testWindow.closePhoneSettings()")


def _delegate_value(engine, window, list_name, child, expression):
    # Delegates are visual children only; findChild cannot see them.
    engine.globalObject().setProperty("delegateList", engine.newQObject(
        _settings_object(window, list_name)))
    return _evaluate(engine, f"""(function () {{
        const delegate = delegateList.itemAtIndex(0)
        function find(item) {{
            if (item.objectName === "{child}") return item
            for (const next of item.children) {{
                const found = find(next)
                if (found) return found
            }}
            return null
        }}
        const item = find(delegate)
        return {expression}
    }})()""")


def test_photos_tab_loads_while_shown_and_hints_without_a_plugin(qml_engine, settings_window):
    window, _bridge = settings_window
    _evaluate(qml_engine, "testWindow.currentTab = testWindow.photosTab")
    QGuiApplication.processEvents()
    assert _evaluate(qml_engine, "testBridge.calls") == [
        {"method": "watchPhotos", "args": [True]},
    ]
    _evaluate(qml_engine, """testBridge.photos = {
        present: false, ready: false, loaded: true,
        hint: "Set up Immich photos with: blueferry plugins immich setup --url https://x"
    }""")
    QGuiApplication.processEvents()
    placeholder = _settings_object(window, "photosPlaceholder")
    assert placeholder.property("visible") is True
    assert placeholder.property("enabled") is False
    assert "plugins immich setup" in placeholder.property("explanation")
    assert _settings_object(window, "photosGrid").property("visible") is False

    _evaluate(qml_engine, """testBridge.photos = {
        present: true, ready: true, loaded: true, hint: "2 recent items",
        items: [{id: "a", label: "<b>2026</b>", video: false, thumbnail: "", original: ""},
                {id: "b", label: "x", video: true, thumbnail: "", original: "file:///tmp/b.mov"}]
    }""")
    QGuiApplication.processEvents()
    assert placeholder.property("visible") is False
    assert _settings_object(window, "photosGrid").property("count") == 2
    source = (ROOT / "src/blueferry/qt/qml/PhotosTab.qml").read_text()
    assert source.count("Controls.Label {") == source.count("textFormat: Text.PlainText")
    _click_control(window, _settings_object(window, "photosRefresh"))
    assert _evaluate(qml_engine, "testBridge.calls")[-1] == {"method": "refreshPhotos", "args": []}

    _evaluate(qml_engine, "testWindow.currentTab = testWindow.messagesTab")
    QGuiApplication.processEvents()
    assert _evaluate(qml_engine, "testBridge.calls")[-1] == {
        "method": "watchPhotos", "args": [False],
    }


def test_settings_plugins_list_store_form_and_confirmation(qml_engine, settings_window):
    window, bridge = settings_window
    bridge.setProperty("status", {"daemon": True})
    bridge.setProperty("pluginSettings", {
        "loaded": True, "busy": "", "message": "", "messageOk": True,
        "plugins": [{"id": "io.example.demo", "name": "<b>Demo</b>", "version": "0.1.0",
                     "capabilities": ["Photos"], "source": "https://git.example.org/demo",
                     "ref": "v0.1.0", "managed": True, "enabled": True, "state": "ok",
                     "stateText": "Ready", "detail": "", "hasConfig": True}],
        "ignored": [], "pending": {}, "config": {},
        "store": {"loaded": True, "problems": [], "items": [
            {"id": "io.example.cal", "name": "Calendar", "description": "Soon.",
             "icon": "view-calendar", "emoji": "", "badges": ["Calendar"],
             "repo": "https://git.example.org/cal", "ref": "", "installedRef": "",
             "screenshot": "", "state": "soon", "stateText": "Coming soon",
             "installable": False},
            {"id": "io.example.new", "name": "New", "description": "New one.",
             "icon": "folder-pictures", "emoji": "", "badges": ["Photos"],
             "repo": "https://git.example.org/new", "ref": "v1.0.0", "installedRef": "",
             "screenshot": "", "state": "install", "stateText": "Install",
             "installable": True}]},
        "indexes": ["https://example.org/index.json"], "defaultIndex": "https://example.org/index.json",
    })
    _open_category(qml_engine, window, "plugins")
    assert ("loadPlugins", []) in [(c["method"], c["args"]) for c in _evaluate(qml_engine, "testBridge.calls")]
    card = _settings_object(window, "plugin_io.example.demo")
    heading = _visual_find(card, "pluginName")
    assert heading.property("text") == "<b>Demo</b>  0.1.0"
    engine = qmlEngine(heading)
    engine.globalObject().setProperty("pluginHeading", engine.newQObject(heading))
    assert _evaluate(engine, "pluginHeading.textFormat === 0") is True
    log_button = _visual_find(card, "pluginLogButton")
    QMetaObject.invokeMethod(log_button, "clicked")
    assert _evaluate(qml_engine, "testBridge.calls")[-1] == {
        "method": "openPluginLog", "args": ["io.example.demo"],
    }
    soon = _settings_object(window, "storeCard_io.example.cal")
    soon_button = _visual_find(soon, "storeAction")
    assert soon_button.property("enabled") is False
    fresh = _settings_object(window, "storeCard_io.example.new")
    button = _visual_find(fresh, "storeAction")
    QMetaObject.invokeMethod(button, "clicked")
    assert _evaluate(qml_engine, "testBridge.calls")[-1] == {
        "method": "installStorePlugin", "args": ["io.example.new"],
    }
    state = bridge.property("pluginSettings")
    state = {**state, "pending": {"kind": "install", "id": "io.example.new", "name": "New",
                                  "rows": [{"label": "Source", "value": "https://git.example.org/new"}]},
             "config": {"id": "io.example.demo", "name": "Demo", "loaded": True, "errors": {},
                        "fields": [{"key": "api_key", "label": "API key", "type": "secret",
                                    "required": True, "help": "", "choices": [],
                                    "minimum": 0, "maximum": 1, "value": "", "stored": True}]}}
    bridge.setProperty("pluginSettings", state)
    QGuiApplication.processEvents()
    dialog = _settings_object(window, "pluginInstallDialog")
    assert dialog.property("opened") or dialog.property("visible")
    secret = _settings_object(window, "configField_api_key")
    assert secret.property("text") == "" and "leave empty" in secret.property("placeholderText")
    _evaluate(qml_engine, "testWindow.closePhoneSettings()")


_MARKUP = "<a href='https://evil.example'>pip</a> & <b>co</b>"
_ESCAPED = "&lt;a href='https://evil.example'&gt;pip&lt;/a&gt; &amp; &lt;b&gt;co&lt;/b&gt;"


def test_plugin_messages_show_markup_as_text(qml_engine, settings_window):
    """git's or pip's last line and an index problem are escaped by
    ui/Notice: the InlineMessage never reads them as markup."""
    window, bridge = settings_window
    bridge.setProperty("status", {"daemon": True})
    bridge.setProperty("pluginSettings", {
        "loaded": True, "busy": "", "message": _MARKUP, "messageOk": False,
        "plugins": [], "ignored": [], "pending": {}, "config": {},
        "store": {"loaded": True, "items": [],
                  "problems": [{"problem": _MARKUP, "index": "https://example.org/i.json"}]},
        "indexes": [], "defaultIndex": "https://example.org/i.json",
    })
    page = _open_category(qml_engine, window, "plugins")
    message = _settings_object(window, "pluginMessage")
    assert message.property("visible") is True and message.property("text") == _ESCAPED
    problem = _visual_find(page, "pluginStoreProblem")
    assert problem.property("text") == _ESCAPED + " (https://example.org/i.json)"
    message.setProperty("visible", False)  # the close button
    QGuiApplication.processEvents()
    assert {"method": "clearPluginMessage", "args": []} in _evaluate(qml_engine, "testBridge.calls")
    _evaluate(qml_engine, "testWindow.closePhoneSettings()")


def test_guided_plugin_form_groups_checks_tests_and_signs_in(qml_engine, settings_window):
    window, bridge = settings_window
    bridge.setProperty("status", {"daemon": True})
    plugin = {"id": "io.example.cloud", "name": "Cloud", "version": "1.0", "capabilities": [],
              "source": "https://git.example.org/cloud", "ref": "", "managed": True,
              "enabled": True, "state": "unconfigured", "stateText": "Needs setup",
              "detail": "", "hasConfig": True}
    config = {
        "id": "io.example.cloud", "name": "Cloud", "loaded": True, "errors": {},
        "groups": [{"name": "account", "label": "Konto", "help": "Your server.",
                    "collapsed": False},
                   {"name": "advanced", "label": "Advanced", "help": "", "collapsed": True}],
        "actions": {"test": True, "login": "nextcloud", "loginLabel": "Sign in with Nextcloud"},
        "status": {},
        "fields": [
            _guided_field("url", "Server URL", "url", required=True, group="account",
                          placeholder="https://cloud.example.com",
                          example="https://cloud.example.com/nextcloud",
                          helpUrl="https://docs.example.com/url"),
            _guided_field("token", "App password", "secret", required=True,
                          group="account", stored=True),
            _guided_field("resize", "Resize", "bool", group="advanced", value=False),
            _guided_field("size", "Size", "int", group="advanced", value=50,
                          showIfKey="resize", showIfValue="true"),
        ],
    }
    bridge.setProperty("pluginSettings", {
        "loaded": True, "busy": "", "message": "", "messageOk": True, "plugins": [plugin],
        "ignored": [], "pending": {}, "config": config,
        "store": {"loaded": True, "problems": [], "items": []}, "indexes": [],
        "defaultIndex": "https://example.org/index.json",
    })
    bridge.setProperty("checkErrors", {"url": "must be an https:// URL"})
    _open_category(qml_engine, window, "plugins")
    form = _settings_object(window, "pluginConfigForm")
    engine = qmlEngine(form)
    engine.globalObject().setProperty("guidedForm", engine.newQObject(form))
    url = _visual_find(form, "configField_url")
    assert url.property("placeholderText") == "e.g. https://cloud.example.com"
    example = _visual_find(form, "configExample_url")
    assert example.property("text") == "Example: https://cloud.example.com/nextcloud"
    secret = _visual_find(form, "configField_token")
    assert "leave empty" in secret.property("placeholderText")
    engine.globalObject().setProperty("guidedSecret", engine.newQObject(secret))
    assert _evaluate(engine, "guidedSecret.echoMode === 2") is True
    QMetaObject.invokeMethod(_visual_find(form, "configReveal_token"), "clicked")
    QGuiApplication.processEvents()
    assert _evaluate(engine, "guidedSecret.echoMode === 0") is True
    QMetaObject.invokeMethod(_visual_find(form, "configHelp_url"), "clicked")
    calls = _evaluate(qml_engine, "testBridge.calls")
    assert calls[-1] == {"method": "openPluginHelp", "args": ["io.example.cloud", "url"]}
    # Advanced starts folded; ShowIf hides "size" while resize is off.
    advanced = _visual_find(form, "configGroup_advanced")
    assert advanced.property("open") is False
    assert _evaluate(engine, "guidedForm.shown('size')") is False
    _evaluate(engine, "guidedForm.set('resize', true)")
    assert _evaluate(engine, "guidedForm.shown('size')") is True
    # A pre-check error shows inline and keeps Save and Test off.
    error = _visual_find(form, "configError_url")
    assert error.property("visible") and error.property("text") == "must be an https:// URL"
    save = _visual_find(form, "pluginConfigSave")
    test = _visual_find(form, "pluginConfigTest")
    assert save.property("enabled") is False and test.property("enabled") is False
    bridge.setProperty("checkErrors", {})
    _evaluate(engine, "guidedForm.set('url', 'https://cloud.example')")
    assert save.property("enabled") is True and test.property("enabled") is True
    QMetaObject.invokeMethod(test, "clicked")
    assert _evaluate(qml_engine, "testBridge.calls")[-1]["method"] == "testPluginConfig"
    sign_in = _visual_find(form, "pluginConfigSignIn")
    assert sign_in.property("text") == "Sign in with Nextcloud"
    QMetaObject.invokeMethod(sign_in, "clicked")
    last = _evaluate(qml_engine, "testBridge.calls")[-1]
    assert last["method"] == "signInPlugin" and last["args"][0] == "io.example.cloud"
    # A pending sign-in swaps the button for Cancel and shows the status line;
    # the typed value survives the state update.
    state = bridge.property("pluginSettings")
    bridge.setProperty("pluginSettings", {**state, "config": {**config, "status": {
        "kind": "login", "ok": True, "pending": True, "text": "Waiting <b>…</b>"}}})
    QGuiApplication.processEvents()
    status = _visual_find(form, "pluginConfigStatus")
    assert status.property("visible") and "&lt;b&gt;" in status.property("text")
    assert _visual_find(form, "pluginConfigSignIn").property("visible") is False
    cancel = _visual_find(form, "pluginConfigCancelSignIn")
    QMetaObject.invokeMethod(cancel, "clicked")
    assert _evaluate(qml_engine, "testBridge.calls")[-1]["method"] == "cancelPluginSignIn"
    assert _visual_find(form, "configField_url").property("text") == "https://cloud.example"
    _evaluate(qml_engine, "testWindow.closePhoneSettings()")


def _guided_field(key, label, kind, **extra):
    row = {"key": key, "label": label, "type": kind, "required": False, "help": "",
           "choices": [], "minimum": 0, "maximum": 100, "value": "", "stored": False,
           "placeholder": "", "example": "", "helpUrl": "", "group": "",
           "showIfKey": "", "showIfValue": ""}
    row.update(extra)
    return row


def test_quickshell_now_playing_renders_the_bridge_view(qml_engine) -> None:
    theme = _component(qml_engine, "data/quickshell/ThemePalette.qml").create()
    component = _component(qml_engine, "data/quickshell/QuickshellNowPlaying.qml")
    strip = component.createWithInitialProperties({"ferryTheme": theme})
    assert strip is not None, component.errors()
    try:
        assert strip.property("visible") is False
        strip.setProperty("view", {
            "visible": True, "playing": True, "title": "<b>Song</b>", "artist": "Band",
            "idle": False, "previous": False, "toggle": True, "next": True,
        })
        summary = strip.findChild(QObject, "nowPlayingSummary")
        assert summary.property("text") == "▶  <b>Song</b> — Band"
        assert strip.findChild(QObject, "nowPlayingPrevious").property("enabled") is False
        toggle = strip.findChild(QObject, "nowPlayingToggle")
        assert toggle.property("enabled") is True
        assert toggle.property("text") == "⏸"

        commands = []
        strip.commandRequested.connect(commands.append)
        assert QMetaObject.invokeMethod(toggle, "clicked")
        assert commands == ["toggle"]

        strip.setProperty("view", {"visible": True, "idle": True})
        assert summary.property("text") == "⏸  Nothing is playing on the iPhone"
        strip.setProperty("view", {"visible": True, "title": "", "artist": ""})
        assert summary.property("text") == "⏸  Unknown title"
    finally:
        strip.deleteLater()
        QGuiApplication.processEvents()


def test_quickshell_now_playing_stays_hidden_during_empty_reconnects(qml_engine) -> None:
    from blueferry.quickshell_bridge import now_playing_view

    theme_component = _component(qml_engine, "data/quickshell/ThemePalette.qml")
    theme = theme_component.create()
    assert theme is not None, theme_component.errors()
    theme.setParent(qml_engine)
    component = _component(qml_engine, "data/quickshell/QuickshellNowPlaying.qml")
    strip = component.createWithInitialProperties({"ferryTheme": theme})
    assert strip is not None, component.errors()
    snapshot = {
        "enabled": True, "available": True, "player": {"name": "Music", "state": "paused"},
        "track": {}, "supported_commands": ["volume-up", "volume-down"],
    }
    theme.setParent(strip)
    window = QQuickWindow()
    window.resize(640, 100)
    strip.setParentItem(window.contentItem())
    window.show()
    QGuiApplication.processEvents()
    try:
        for available in (False, True, False, True):
            strip.setProperty("view", now_playing_view({**snapshot, "available": available}))
            assert not strip.property("visible")

        track = {"title": "A podcast", "artist": "A host"}
        strip.setProperty("view", now_playing_view({
            **snapshot, "track": track, "supported_commands": ["play"],
        }))
        assert strip.property("visible")
        assert strip.findChild(QObject, "nowPlayingSummary").property("text") == (
            "⏸  A podcast — A host"
        )
        assert strip.findChild(QObject, "nowPlayingToggle").property("enabled")

        strip.setProperty("view", now_playing_view(snapshot))
        assert not strip.property("visible")
    finally:
        window.close()
        strip.deleteLater()
        window.deleteLater()
        QGuiApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def test_quickshell_fetches_now_playing_only_with_the_opt_in(qml_engine) -> None:
    source = (ROOT / "data/quickshell/shell.qml").read_text()
    functions = _qml_functions(source, ("refreshNowPlaying",))
    script = '''(function() {
        var calls = [];
        var backendBridge = {
            requestLatest: function(method) { calls.push("request:" + method); },
            cancelLatest: function(method) { calls.push("cancel:" + method); }
        };
        var root = {backendStatus: {media_control_enabled: "true"}, nowPlaying: {visible: true}};
    ''' + functions + '''
        refreshNowPlaying();
        if (root.nowPlaying.visible) throw new Error("stale strip kept");
        root.backendStatus = {media_control_enabled: true};
        refreshNowPlaying();
        return JSON.stringify(calls);
    })()'''
    assert _evaluate(qml_engine, script) == '["cancel:now_playing","request:now_playing"]'
    assert 'name === "now-playing-changed") root.refreshNowPlaying()' in source


def test_quickshell_discards_late_media_after_status_loss(qml_engine) -> None:
    source = (ROOT / "data/quickshell/shell.qml").read_text()
    functions = _qml_functions(source, ("markStatusUnavailable", "applyNowPlaying"))
    script = '''(function() {
        var calls = [];
        var backendBridge = {cancelLatest: function(method) { calls.push(method); }};
        var backendStatus = {media_control_enabled: true};
        var nowPlaying = {visible: true};
        var statusErrorText = "";
        var root = {};
        Object.defineProperty(root, "backendStatus", {get: function() {return backendStatus;}});
        Object.defineProperty(root, "nowPlaying", {
            get: function() {return nowPlaying;}, set: function(value) {nowPlaying = value;}
        });
    ''' + functions + '''
        markStatusUnavailable("gone");
        applyNowPlaying({visible: true, title: "old"});
        if (nowPlaying.visible) throw new Error("late media restored after status loss");
        backendStatus = {media_control_enabled: false};
        applyNowPlaying({visible: true});
        if (nowPlaying.visible) throw new Error("late media restored after opt-out");
        backendStatus = {media_control_enabled: true};
        applyNowPlaying({visible: true});
        if (!nowPlaying.visible) throw new Error("fresh media was hidden");
        return JSON.stringify(calls);
    })()'''
    assert _evaluate(qml_engine, script) == '["now_playing"]'


def test_quickshell_empty_activation_only_presents_the_window(qml_engine) -> None:
    source = (ROOT / "data/quickshell/shell.qml").read_text()
    functions = _qml_functions(source, ("openMessage",))
    script = '''(function() {
        var calls = [];
        var pendingMessageHandle = "earlier";
        function presentWindow() { calls.push("present"); }
        function selectMessage(handle) { calls.push("select:" + handle); return false; }
        function reload() { calls.push("reload"); }
    ''' + functions + '''
        openMessage("");
        if (pendingMessageHandle !== "earlier") throw new Error("pending handle replaced");
        openMessage("m1");
        return JSON.stringify(calls);
    })()'''
    assert _evaluate(qml_engine, script) == '["present","present","select:m1","reload"]'
