pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

// Confirmation for a clicked tel:/callto: link. The link comes from any
// application, so this dialog only ever dials after the user chose Call.
// The number was validated by the bridge (tel_links.dial_request); the
// contact name is untrusted phone data and is shown as plain text only.
Kirigami.Dialog {
    id: dialog
    objectName: "dialLinkDialog"
    required property var bridge
    property string number: ""
    // "", "invalid" or "emergency" (see tel_links.dial_request).
    property string problem: ""
    property string contactName: ""
    readonly property var status: dialog.bridge.status || ({})
    readonly property bool callsEnabled: dialog.status.calls_enabled === true
    readonly property string callsState: dialog.bridge.callsState || "disabled"
    readonly property bool canCall: dialog.problem === "" && dialog.callsEnabled
        && dialog.callsState === "ready"
    readonly property bool offerSettings: dialog.problem === "" && !dialog.canCall

    signal phoneSettingsRequested()

    function request(number: string, problem: string): void {
        dialog.number = number
        dialog.problem = problem
        dialog.contactName = number !== "" ? dialog.bridge.contactNameForNumber(number) : ""
        dialog.open()
    }

    function bodyText(): string {
        if (dialog.problem === "invalid")
            return qsTr("This link does not contain a phone number BlueFerry can call. "
                + "Only plain numbers (an optional + and digits) are dialed.")
        if (dialog.problem === "emergency")
            return qsTr("Emergency numbers are not dialed through BlueFerry. "
                + "Call %1 on the iPhone itself.").arg(dialog.number)
        if (dialog.status.daemon === false)
            return qsTr("BlueFerry's background service is not reachable, so %1 "
                + "cannot be called now.").arg(dialog.number)
        if (!dialog.callsEnabled)
            return qsTr("Calling %1 needs phone calls through this computer, which are "
                + "turned off. Turn them on under Phone Calls in the iPhone settings.")
                .arg(dialog.number)
        if (!dialog.canCall) {
            if (dialog.callsState === "connecting" || dialog.callsState === "searching")
                return qsTr("Phone calls are still connecting to the iPhone. "
                    + "You can call %1 as soon as they are ready.").arg(dialog.number)
            return qsTr("Phone calls are on, but the iPhone's hands-free connection is "
                + "not available (%1). See \"Phone calls\" in the BlueFerry documentation.")
                .arg(dialog.callsState)
        }
        if (dialog.contactName !== "")
            return qsTr("Call %1 from your iPhone?").arg(dialog.contactName)
        return qsTr("Call %1 from your iPhone?").arg(dialog.number)
    }

    title: dialog.problem !== "" ? qsTr("Can't Call This Number")
        : dialog.canCall ? qsTr("Place Call?") : qsTr("Phone Calls Unavailable")
    preferredWidth: Kirigami.Units.gridUnit * 24
    padding: Kirigami.Units.largeSpacing
    standardButtons: dialog.canCall || dialog.offerSettings
        ? Kirigami.Dialog.Cancel : Kirigami.Dialog.Close
    customFooterActions: [
        Kirigami.Action {
            text: qsTr("Open iPhone Settings")
            icon.name: "phone"
            visible: dialog.offerSettings
            onTriggered: {
                dialog.close()
                dialog.phoneSettingsRequested()
            }
        },
        Kirigami.Action {
            text: qsTr("Call")
            icon.name: "call-start"
            visible: dialog.canCall
            enabled: !dialog.bridge.busy
            onTriggered: {
                dialog.bridge.dialCall(dialog.number)
                dialog.close()
            }
        }
    ]

    Connections {
        target: dialog.bridge

        // A link opened at startup can arrive before the conversations.
        function onThreadsChanged() {
            if (dialog.opened && dialog.number !== "")
                dialog.contactName = dialog.bridge.contactNameForNumber(dialog.number)
        }
    }

    ColumnLayout {
        spacing: Kirigami.Units.smallSpacing

        Controls.Label {
            objectName: "dialLinkText"
            Layout.fillWidth: true
            text: dialog.bodyText()
            wrapMode: Text.WordWrap
            textFormat: Text.PlainText
        }
        Controls.Label {
            objectName: "dialLinkNumber"
            Layout.fillWidth: true
            visible: dialog.canCall && dialog.contactName !== ""
            text: dialog.number
            font.bold: true
            textFormat: Text.PlainText
        }
    }
}
