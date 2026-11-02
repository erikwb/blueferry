pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import "ui"

// Calls tab: running calls as cards, a dial pad for the optional HFP
// integration, and the optional recent-calls list. The pad and the list stay
// visible but disabled when their opt-in is off.
GridLayout {
    id: callsTab
    objectName: "callsTab"
    required property var bridge
    signal messageRequested(string address)

    readonly property var hints: callsTab.bridge.featureHints || ({})
    readonly property bool callsEnabled: (callsTab.bridge.status || {}).calls_enabled === true
    readonly property bool callsReady: callsTab.bridge.callsState === "ready"
    readonly property var phoneCalls: callsTab.bridge.phoneCalls || []
    // The call keypad tones go to (phone_link_view.active_call_id).
    readonly property string activeCall: callsTab.bridge.activeCallId || ""
    // A call just connected: an empty number field must not swallow the
    // keyboard's keypad keys meant for the call's menu.
    onActiveCallChanged: {
        if (callsTab.activeCall !== "" && dialField.activeFocus && dialField.text === "")
            dialField.focus = false
    }

    function append(key: string): void {
        dialField.text += key
    }

    // A pad key: a tone for the active call, else part of the number.
    function padKey(key: string): void {
        if (callsTab.activeCall === "" || !callsTab.bridge.pressCallTone(key))
            callsTab.append(key)
    }

    function dial(): void {
        // Preserve the input for validation and confirmation. Discarding
        // letters or extensions here could turn it into a different number.
        if (dialField.text.trim() !== "" && callsTab.callsReady && !callsTab.bridge.busy)
            callsTab.bridge.requestDialLink(dialField.text)
    }

    // Why the pad cannot call (phone_link_view.calls_hint); both arguments
    // are bound so the text follows the status and the Calls1 state.
    readonly property string notReadyText: callsTab.bridge.callsHint(
        callsTab.bridge.status || ({}), callsTab.bridge.callsState || "")

    // Seconds since a call was first seen, for the running-call cards.
    property double now: Date.now()
    Timer {
        interval: 1000
        repeat: true
        running: callsTab.phoneCalls.length > 0 && callsTab.visible
        onTriggered: callsTab.now = Date.now()
    }

    columns: width > Kirigami.Units.gridUnit * 36 ? 2 : 1
    columnSpacing: 0
    rowSpacing: 0
    Layout.minimumHeight: 0

    Controls.ScrollView {
        id: dialerScroll
        objectName: "dialerScroll"
        // Size the viewport from the layout, independently of its content.
        implicitWidth: 0
        implicitHeight: 0
        Layout.preferredWidth: Kirigami.Units.gridUnit * 16
        Layout.preferredHeight: Kirigami.Units.gridUnit * 24
        Layout.maximumWidth: callsTab.columns === 1 ? -1 : Kirigami.Units.gridUnit * 16
        Layout.fillWidth: callsTab.columns === 1
        Layout.fillHeight: true
        Layout.minimumHeight: 0
        Layout.margins: Kirigami.Units.largeSpacing
        clip: true
        contentWidth: availableWidth
        contentHeight: dialerColumn.implicitHeight
        Controls.ScrollBar.horizontal.policy: Controls.ScrollBar.AlwaysOff

        ColumnLayout {
            id: dialerColumn
            width: dialerScroll.availableWidth
            spacing: Kirigami.Units.largeSpacing

            // Running calls: only while one exists.
            Repeater {
                model: callsTab.phoneCalls
                delegate: CardFrame {
                    id: activeCall
                    required property var modelData
                    // Wording from phone_link_view.call_card; "now" ticks the duration.
                    readonly property var card: callsTab.bridge.callCard(
                        activeCall.modelData, callsTab.now) || ({})
                    objectName: "activeCallCard"
                    Layout.fillWidth: true
                    title: activeCall.card.title || ""
                    subtitleItem.objectName: "activeCallState"
                    subtitle: activeCall.card.subtitle || ""
                    leading: Component {
                        ContactAvatar {
                            bridge: callsTab.bridge
                            address: activeCall.modelData.number || ""
                        }
                    }
                    actions: [
                        Controls.Button {
                            visible: activeCall.card.ringing === true
                            text: activeCall.card.answer || ""
                            icon.name: "call-start"
                            enabled: !callsTab.bridge.busy
                            onClicked: callsTab.bridge.answerCall(activeCall.modelData.call_id)
                        },
                        Controls.Button {
                            visible: activeCall.card.ringing !== true
                            text: activeCall.card.hold || ""
                            icon.name: activeCall.card.held === true
                                ? "media-playback-start" : "media-playback-pause"
                            enabled: !callsTab.bridge.busy
                            onClicked: callsTab.bridge.swapCalls()
                        },
                        Controls.Button {
                            objectName: "activeCallHangup"
                            text: activeCall.card.hangup || ""
                            icon.name: "call-stop"
                            enabled: !callsTab.bridge.busy
                            palette.button: Kirigami.Theme.negativeBackgroundColor
                            onClicked: callsTab.bridge.hangupCall(activeCall.modelData.call_id)
                        }
                    ]
                    // Keypad tones (DTMF) go to the active call only. The text
                    // is kept by the bridge: the cards are rebuilt on each
                    // phoneCalls change.
                    RowLayout {
                        objectName: "callTonesRow"
                        Layout.fillWidth: true
                        visible: activeCall.card.active === true
                        Controls.TextField {
                            id: tonesField
                            objectName: "callTonesField"
                            Layout.fillWidth: true
                            placeholderText: activeCall.card.tones || ""
                            Accessible.name: placeholderText
                            maximumLength: 32
                            inputMethodHints: Qt.ImhDialableCharactersOnly
                            validator: RegularExpressionValidator { regularExpression: /[0-9*#]*/ }
                            text: callsTab.bridge.callTonesDraft || ""
                            onTextEdited: callsTab.bridge.setCallTonesDraft(text)
                            onAccepted: activeCall.sendTones()
                        }
                        Controls.Button {
                            objectName: "callTonesSend"
                            text: activeCall.card.send_tones || ""
                            icon.name: "input-dialpad"
                            // Tones queue up on their own; other actions do not block them.
                            enabled: tonesField.text !== ""
                            onClicked: activeCall.sendTones()
                        }
                    }
                    function sendTones(): void {
                        if (tonesField.text !== "")
                            callsTab.bridge.sendCallTones(activeCall.modelData.call_id, tonesField.text)
                    }
                    // The tones sent so far ("Sent: 1 2"), from the pad, the
                    // keyboard or the field above.
                    Controls.Label {
                        objectName: "callTonesSent"
                        Layout.fillWidth: true
                        visible: activeCall.card.active === true && text !== ""
                        text: callsTab.bridge.callTonesSent || ""
                        textFormat: Text.PlainText
                        opacity: 0.7
                    }
                }
            }

            Kirigami.InlineMessage {
                objectName: "callsHint"
                Layout.fillWidth: true
                visible: !callsTab.callsEnabled || !callsTab.callsReady
                type: Kirigami.MessageType.Information
                text: callsTab.notReadyText
            }

            ColumnLayout {
                Layout.fillWidth: true
                spacing: Kirigami.Units.largeSpacing
                enabled: callsTab.callsEnabled

                // Large number display; letters search the contacts.
                RowLayout {
                    Layout.fillWidth: true
                    Controls.TextField {
                        id: dialField
                        objectName: "dialPadField"
                        Layout.fillWidth: true
                        horizontalAlignment: Text.AlignHCenter
                        font.pointSize: Kirigami.Theme.defaultFont.pointSize * 1.8
                        background: null
                        placeholderText: qsTr("Number or name")
                        Accessible.name: qsTr("Phone number or contact name")
                        onTextEdited: searchTimer.restart()
                        onAccepted: callsTab.dial()
                    }
                    Controls.ToolButton {
                        objectName: "dialDeleteKey"
                        icon.name: "edit-clear"
                        text: qsTr("Delete")
                        display: Controls.AbstractButton.IconOnly
                        visible: dialField.text !== ""
                        Accessible.name: text
                        Controls.ToolTip.text: text
                        Controls.ToolTip.visible: hovered
                        onClicked: dialField.text = dialField.text.slice(0, -1)
                        onPressAndHold: dialField.text = ""
                    }
                }
                Timer {
                    id: searchTimer
                    interval: 200
                    onTriggered: callsTab.bridge.findContacts(
                        dialField.text.trim().length >= 2 ? dialField.text.trim() : "")
                }
                ListView {
                    id: suggestions
                    objectName: "dialSuggestions"
                    Layout.fillWidth: true
                    Layout.preferredHeight: count > 0 ? Math.min(contentHeight, Kirigami.Units.gridUnit * 8) : 0
                    visible: count > 0 && dialField.text.trim().length >= 2
                    clip: true
                    model: callsTab.bridge.dialSuggestions || []
                    delegate: ListRow {
                        id: suggestion
                        required property var modelData
                        density: "compact"
                        title: suggestion.modelData.name
                        subtitle: suggestion.modelData.address
                        leading: Component {
                            ContactAvatar {
                                bridge: callsTab.bridge
                                address: suggestion.modelData.address
                            }
                        }
                        onClicked: {
                            dialField.text = suggestion.modelData.address
                            callsTab.bridge.findContacts("")
                        }
                    }
                }

                GridLayout {
                    objectName: "dialPad"
                    Layout.alignment: Qt.AlignHCenter
                    columns: 3
                    columnSpacing: Kirigami.Units.largeSpacing * 2
                    rowSpacing: Kirigami.Units.largeSpacing

                    Repeater {
                        model: callsTab.bridge.dialKeys || []
                        delegate: Controls.AbstractButton {
                            id: key
                            required property var modelData
                            objectName: "dialKey" + key.modelData[0]
                            implicitWidth: Kirigami.Units.gridUnit * 3.4
                            implicitHeight: implicitWidth
                            Accessible.name: qsTr("Dial %1").arg(key.modelData[0])
                            background: Rectangle {
                                radius: width / 2
                                color: key.down ? Kirigami.Theme.highlightColor
                                    : key.hovered ? Qt.alpha(Kirigami.Theme.highlightColor, 0.25)
                                    : Kirigami.Theme.alternateBackgroundColor
                                border.width: key.visualFocus ? 2 : 0
                                border.color: Kirigami.Theme.focusColor
                            }
                            contentItem: ColumnLayout {
                                spacing: 0
                                Item { Layout.fillHeight: true }
                                Controls.Label {
                                    Layout.alignment: Qt.AlignHCenter
                                    text: key.modelData[0]
                                    textFormat: Text.PlainText
                                    font.pointSize: Kirigami.Theme.defaultFont.pointSize * 1.6
                                }
                                Controls.Label {
                                    Layout.alignment: Qt.AlignHCenter
                                    text: key.modelData[1]
                                    textFormat: Text.PlainText
                                    font: Kirigami.Theme.smallFont
                                    opacity: 0.7
                                    visible: text !== ""
                                }
                                Item { Layout.fillHeight: true }
                            }
                            // Qt suppresses clicked after a handled long press.
                            // A long press on 0 dials "+"; during a call it is just 0.
                            onPressAndHold: {
                                if (key.modelData[0] === "0") {
                                    if (callsTab.activeCall !== "")
                                        callsTab.padKey("0")
                                    else
                                        callsTab.append("+")
                                }
                            }
                            onClicked: callsTab.padKey(key.modelData[0])
                        }
                    }
                }

                Controls.RoundButton {
                    id: callButton
                    objectName: "dialPadCallButton"
                    Layout.alignment: Qt.AlignHCenter
                    implicitWidth: Kirigami.Units.gridUnit * 3.4
                    implicitHeight: implicitWidth
                    icon.name: "call-start"
                    icon.color: "white"
                    icon.width: Kirigami.Units.iconSizes.medium
                    icon.height: Kirigami.Units.iconSizes.medium
                    text: qsTr("Call")
                    display: Controls.AbstractButton.IconOnly
                    enabled: callsTab.callsReady && dialField.text.trim() !== "" && !callsTab.bridge.busy
                    opacity: enabled ? 1 : 0.45
                    background: Rectangle {
                        radius: width / 2
                        color: callButton.down ? Qt.darker(Kirigami.Theme.positiveTextColor, 1.2)
                            : Kirigami.Theme.positiveTextColor
                    }
                    Accessible.name: text
                    // Disabled buttons get no hover; the hint above explains why.
                    Controls.ToolTip.text: callsTab.callsReady ? text : callsTab.notReadyText
                    Controls.ToolTip.visible: hovered
                    onClicked: callsTab.dial()
                }
            }
        }
    }

    ColumnLayout {
        Layout.fillWidth: true
        Layout.fillHeight: true
        // Keep recent calls usable below the scrollable dialer in a narrow
        // window, even with several running calls above the number field.
        Layout.minimumHeight: callsTab.columns === 1
            ? Kirigami.Units.gridUnit * 8 : 0
        Layout.preferredHeight: Kirigami.Units.gridUnit * 14
        spacing: 0

        Loader {
            Layout.fillWidth: true
            Layout.fillHeight: true
            active: callsTab.bridge.callHistoryEnabled === true
            visible: active
            sourceComponent: RecentCallsPage {
                bridge: callsTab.bridge
                onCallRequested: number => callsTab.bridge.requestDialLink(number)
                onMessageRequested: address => callsTab.messageRequested(address)
            }
        }

        Item {
            Layout.fillWidth: true
            Layout.fillHeight: true
            visible: callsTab.bridge.callHistoryEnabled !== true
            EmptyState {
                anchors.centerIn: parent
                objectName: "callHistoryHint"
                dimmed: true
                icon.name: "call-start"
                text: qsTr("Recent Calls Are Off")
                explanation: callsTab.hints.callHistory || ""
            }
        }
    }
}
