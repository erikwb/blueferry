pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

// Confirmation for a dial-pad entry, recent call, or clicked tel:/callto:
// link. This dialog only ever dials after the user chose Call.
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
    // What the dialog offers is fixed when it opens, so a button never turns
    // into Call under a pending click. Live changes may only take Call away.
    // "invalid", "emergency", "unreachable", "off", "connecting",
    // "unavailable" or "ready".
    property string kind: ""
    property string stateCode: ""
    // The next request; it waits for the first status and for an open
    // dialog to close, so a number is never swapped under the cursor.
    property var pending: null
    readonly property var status: dialog.bridge.status || ({})
    readonly property bool liveReady: dialog.problem === "" && dialog.status.daemon === true
        && dialog.status.calls_enabled === true && dialog.status.calls_state === "ready"
    readonly property bool canCall: dialog.kind === "ready" && dialog.liveReady
    // A ready dialog that lost its calls explains why, without new buttons.
    readonly property string shownKind: dialog.kind === "ready" && !dialog.liveReady
        ? dialog.liveKind() : dialog.kind
    readonly property bool offerSettings: ["unreachable", "off", "connecting", "unavailable"]
        .indexOf(dialog.kind) >= 0

    signal phoneSettingsRequested()

    function request(number: string, problem: string): void {
        dialog.pending = {number: number, problem: problem}
        dialog.presentPending()
    }

    function presentPending(): void {
        const next = dialog.pending
        if (!next)
            return
        if (dialog.visible) {
            dialog.close()  // reopens from onClosed
            return
        }
        // Without a status yet every call would look unavailable.
        if (next.problem === "" && dialog.status.daemon === undefined)
            return
        dialog.pending = null
        dialog.number = next.number
        dialog.problem = next.problem
        dialog.contactName = next.number !== ""
            ? dialog.bridge.contactNameForNumber(next.number) : ""
        dialog.kind = dialog.liveKind()
        dialog.stateCode = String(dialog.status.calls_state || "")
        dialog.open()
    }

    function liveKind(): string {
        if (dialog.problem !== "")
            return dialog.problem
        if (dialog.status.daemon !== true)
            return "unreachable"
        if (dialog.status.calls_enabled !== true)
            return "off"
        const state = String(dialog.status.calls_state || "")
        if (state === "ready")
            return "ready"
        return state === "connecting" || state === "searching" ? "connecting" : "unavailable"
    }

    function bodyText(): string {
        switch (dialog.shownKind) {
        case "invalid":
            return qsTr("This does not contain a phone number BlueFerry can call. "
                + "Only plain numbers (an optional + and digits) are dialed.")
        case "emergency":
            return qsTr("Emergency numbers are not dialed through BlueFerry. "
                + "Call %1 on the iPhone itself.").arg(dialog.number)
        case "unreachable":
            return qsTr("BlueFerry's background service is not reachable, so %1 "
                + "cannot be called now.").arg(dialog.number)
        case "off":
            return qsTr("Calling %1 needs phone calls through this computer, which are "
                + "turned off. Turn them on under Phone Calls in the iPhone settings.")
                .arg(dialog.number)
        case "connecting":
            return qsTr("Phone calls are still connecting to the iPhone. "
                + "You can call %1 as soon as they are ready.").arg(dialog.number)
        case "unavailable":
            return qsTr("Phone calls are on, but the iPhone's hands-free connection is "
                + "not available (%1). See \"Phone calls\" in the BlueFerry documentation.")
                .arg(dialog.kind === "ready" ? String(dialog.status.calls_state || "")
                    : dialog.stateCode)
        }
        if (dialog.contactName !== "")
            return qsTr("Call %1 from your iPhone?").arg(dialog.contactName)
        return qsTr("Call %1 from your iPhone?").arg(dialog.number)
    }

    title: dialog.problem !== "" ? qsTr("Can't Call This Number")
        : dialog.canCall ? qsTr("Place Call?") : qsTr("Phone Calls Unavailable")
    preferredWidth: Kirigami.Units.gridUnit * 24
    padding: Kirigami.Units.largeSpacing
    standardButtons: dialog.kind === "ready" || dialog.offerSettings
        ? Kirigami.Dialog.Cancel : Kirigami.Dialog.Close
    onClosed: dialog.presentPending()
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
                if (dialog.canCall)
                    dialog.bridge.dialCall(dialog.number)
                dialog.close()
            }
        }
    ]

    Connections {
        target: dialog.bridge

        function onStatusChanged() {
            dialog.presentPending()
        }

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
