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

from PySide6.QtCore import Property, QMetaObject, QObject, QPointF, Qt, QUrl, Slot
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtQml import QQmlComponent, QQmlEngine
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
            property var status: ({})
            property var threads: []
            property var devices: []
            property var contactResults: []
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
            signal pairingConfirmationRequested(string passkey)
            signal messageOpenRequested(string handle)
            signal messageSendSucceeded(string recipient, string body)
            signal threadSendSucceeded(string key, string body)
            signal groupConfirmationRequested(string key, string body, string recipients)
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
            function forgetDevice(mac) { record("forgetDevice", [mac]); }
            function activateBluetooth() { record("activateBluetooth", []); }
            function filePairingIssue() { record("filePairingIssue", []); }
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


def _settings_object(window, name):
    obj = window.findChild(QObject, name)
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
        _click_control(window, _settings_object(window, "pairPhoneButton"))
        if replace:
            dialog = _settings_object(window, "replaceTargetDialog")
            assert dialog.property("visible")
            qml_engine.globalObject().setProperty("pairingDialog", qml_engine.newQObject(dialog))
            _evaluate(qml_engine, "pairingDialog.customFooterActions[0].trigger()")
        assert len(operations) == 1
        operations[0]()
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
], ids=["first-install", "adapter-switch", "cancel-scan", "failed-pair", "replacement-snapshot",
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


@pytest.mark.private_dbus
@pytest.mark.parametrize("late_read", ["success", "failure"])
@pytest.mark.parametrize("late_after_refresh", [False, True])
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
