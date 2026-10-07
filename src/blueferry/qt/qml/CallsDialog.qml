pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

// Optional, experimental phone calls (opt-in in the iPhone settings). All
// actions go through the bridge; this component performs no I/O itself.
Kirigami.Dialog {
    id: dialog
    required property var bridge
    readonly property var phoneCalls: dialog.bridge.phoneCalls || []
    readonly property string callsState: dialog.bridge.callsState || "disabled"
    readonly property bool callsReady: dialog.callsState === "ready"
    title: qsTr("Phone Calls")
    preferredWidth: Kirigami.Units.gridUnit * 26
    standardButtons: Kirigami.Dialog.Close
    customFooterActions: [
        Kirigami.Action {
            text: qsTr("Hang Up All")
            icon.name: "call-stop"
            enabled: dialog.phoneCalls.length > 1 && !dialog.bridge.busy
            onTriggered: dialog.bridge.hangupAllCalls()
        },
        Kirigami.Action {
            id: dialAction
            text: qsTr("Dial")
            icon.name: "call-start"
            enabled: dialog.callsReady && dialNumber.text.trim() !== "" && !dialog.bridge.busy
            // Ask first: a stray Enter in the number field must not place a
            // call (emergency numbers are refused by the backend).
            onTriggered: {
                dialConfirm.number = dialNumber.text.trim()
                dialConfirm.open()
            }
        }
    ]

    function stateText(state: string): string {
        switch (state) {
        case "ready": return qsTr("Ready")
        case "connecting": return qsTr("Bringing the iPhone's hands-free connection online…")
        case "searching": return qsTr("Waiting for the iPhone's hands-free modem in oFono…")
        case "unavailable": return qsTr("oFono is not running.")
        case "bluez_conflict": return qsTr("BlueZ's own HFP plugin holds the iPhone's call channel. Start bluetoothd with -P hfp (keep -E), then reconnect the iPhone.")
        default: return qsTr("Phone calls are disabled.")
        }
    }

    onOpened: {
        dialog.bridge.refreshCalls()
        dialNumber.forceActiveFocus()
    }

    ColumnLayout {
        spacing: Kirigami.Units.smallSpacing

        Kirigami.PromptDialog {
            id: dialConfirm
            objectName: "callsDialConfirm"
            property string number: ""
            title: qsTr("Place Call?")
            subtitle: qsTr("Call %1 through the iPhone?").arg(dialConfirm.number)
            standardButtons: Kirigami.Dialog.Cancel
            customFooterActions: [Kirigami.Action {
                text: qsTr("Call")
                icon.name: "call-start"
                onTriggered: {
                    dialog.bridge.dialCall(dialConfirm.number)
                    dialConfirm.close()
                }
            }]
        }

        Controls.Label {
            Layout.fillWidth: true
            text: dialog.stateText(dialog.callsState)
            wrapMode: Text.WordWrap
            textFormat: Text.PlainText
        }

        Controls.Label {
            visible: dialog.phoneCalls.length === 0
            text: qsTr("No active calls")
            opacity: 0.7
        }

        Repeater {
            model: dialog.phoneCalls

            delegate: RowLayout {
                id: callRow
                required property var modelData
                Layout.fillWidth: true
                spacing: Kirigami.Units.smallSpacing

                Controls.Label {
                    Layout.fillWidth: true
                    text: callRow.modelData.display_peer + "\n" + callRow.modelData.state
                    textFormat: Text.PlainText
                    wrapMode: Text.Wrap
                    maximumLineCount: 3
                    elide: Text.ElideRight
                }
                Controls.Button {
                    visible: callRow.modelData.ringing === true
                    text: qsTr("Answer")
                    icon.name: "call-start"
                    enabled: !dialog.bridge.busy
                    onClicked: dialog.bridge.answerCall(callRow.modelData.call_id)
                }
                Controls.Button {
                    text: callRow.modelData.ringing === true ? qsTr("Decline") : qsTr("Hang Up")
                    icon.name: "call-stop"
                    enabled: !dialog.bridge.busy
                    onClicked: dialog.bridge.hangupCall(callRow.modelData.call_id)
                }
            }
        }

        Controls.Label {
            text: qsTr("Number")
            font.bold: true
        }
        Controls.TextField {
            id: dialNumber
            objectName: "callsNumberField"
            Layout.fillWidth: true
            enabled: dialog.callsReady
            maximumLength: 96
            placeholderText: qsTr("Phone number")
            Accessible.name: qsTr("Number to dial")
            inputMethodHints: Qt.ImhDialableCharactersOnly
            onAccepted: {
                if (dialAction.enabled)
                    dialAction.trigger()
            }
        }
    }
}
