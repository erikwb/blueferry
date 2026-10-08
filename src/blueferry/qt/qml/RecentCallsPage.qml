pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

// Opt-in list of the iPhone's recent calls. Main.qml only instantiates it
// through a Loader when the backend reports call_history_enabled.
Kirigami.ScrollablePage {
    id: callsPage
    objectName: "recentCallsPage"
    title: qsTr("Recent Calls")
    required property var bridge
    property bool missedOnly: false
    signal closeRequested()

    function visibleCalls() {
        const calls = callsPage.bridge.callHistory || []
        if (!callsPage.missedOnly)
            return calls
        return calls.filter(call => call.missed === true)
    }

    function directionText(direction) {
        if (direction === "missed")
            return qsTr("Missed")
        if (direction === "outgoing")
            return qsTr("Outgoing")
        return qsTr("Incoming")
    }

    function directionIcon(direction) {
        if (direction === "missed")
            return "call-missed"
        if (direction === "outgoing")
            return "call-outgoing"
        return "call-incoming"
    }

    actions: [
        Kirigami.Action {
            text: qsTr("Missed Only")
            icon.name: "call-missed"
            checkable: true
            checked: callsPage.missedOnly
            onToggled: callsPage.missedOnly = checked
        },
        Kirigami.Action {
            text: qsTr("Refresh from iPhone")
            icon.name: "view-refresh"
            enabled: !callsPage.bridge.busy
            onTriggered: callsPage.bridge.syncCallHistory()
        },
        Kirigami.Action {
            text: qsTr("Close")
            icon.name: "window-close"
            onTriggered: callsPage.closeRequested()
        }
    ]

    ListView {
        id: callsList
        model: callsPage.visibleCalls()
        reuseItems: true

        Kirigami.PlaceholderMessage {
            anchors.centerIn: parent
            width: parent.width - Kirigami.Units.gridUnit * 4
            visible: callsList.count === 0
            icon.name: callsPage.bridge.callHistoryError !== "" ? "dialog-warning" : "call-start"
            text: callsPage.bridge.callHistoryError !== ""
                ? qsTr("Call history is unavailable")
                : (callsPage.missedOnly ? qsTr("No missed calls") : qsTr("No recent calls"))
            explanation: callsPage.bridge.callHistoryError
        }

        delegate: Controls.ItemDelegate {
            id: callRow
            required property var modelData
            width: ListView.view.width
            Accessible.name: callsPage.directionText(callRow.modelData.direction)
                + ", " + callRow.modelData.caller + ", " + callRow.modelData.time

            contentItem: RowLayout {
                spacing: Kirigami.Units.largeSpacing

                Kirigami.Icon {
                    source: callsPage.directionIcon(callRow.modelData.direction)
                    color: callRow.modelData.missed ? Kirigami.Theme.negativeTextColor
                                                    : Kirigami.Theme.textColor
                    Layout.preferredWidth: Kirigami.Units.iconSizes.smallMedium
                    Layout.preferredHeight: Kirigami.Units.iconSizes.smallMedium
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 0

                    Controls.Label {
                        Layout.fillWidth: true
                        // Remote names are plain text, never markup.
                        textFormat: Text.PlainText
                        text: callRow.modelData.caller
                        elide: Text.ElideRight
                        color: callRow.modelData.missed ? Kirigami.Theme.negativeTextColor
                                                        : Kirigami.Theme.textColor
                    }
                    Controls.Label {
                        Layout.fillWidth: true
                        textFormat: Text.PlainText
                        text: callRow.modelData.name !== "" && callRow.modelData.address !== ""
                            ? callsPage.directionText(callRow.modelData.direction) + " · "
                                + callRow.modelData.address
                            : callsPage.directionText(callRow.modelData.direction)
                        elide: Text.ElideRight
                        opacity: 0.7
                    }
                }

                Controls.Label {
                    textFormat: Text.PlainText
                    text: callRow.modelData.time
                    opacity: 0.7
                }
            }
        }
    }
}
