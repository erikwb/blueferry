pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import "ui"

// Opt-in list of the iPhone's recent calls, embedded in the Calls tab. Main
// only instantiates it through a Loader when the backend reports
// call_history_enabled, and records are fetched only while the tab is shown.
// Rows come pre-grouped from the controller (phone_overview.call_groups):
// one section per day, repeated calls from one number folded with a count.
ColumnLayout {
    id: callsPage
    objectName: "recentCallsPage"
    required property var bridge
    property bool missedOnly: false
    // A call picked in the global search: its row is shown and marked.
    property string focusedCallId: ""
    readonly property bool canCall: (callsPage.bridge.status || ({})).calls_enabled === true
        && callsPage.bridge.callsState === "ready"
    signal callRequested(string number)
    signal messageRequested(string address)
    spacing: 0

    function visibleCalls() {
        const rows = callsPage.bridge.callHistoryRows || ({})
        return (callsPage.missedOnly ? rows.missed : rows.all) || []
    }

    // Row of the focused call among the visible rows, or -1.
    function focusedIndex(): int {
        if (callsPage.focusedCallId === "")
            return -1
        const rows = callsPage.visibleCalls()
        for (let index = 0; index < rows.length; ++index) {
            if ((rows[index].ids || []).indexOf(callsPage.focusedCallId) >= 0)
                return index
        }
        return -1
    }

    // Computed once per change of the list or the focused call, not per row.
    readonly property int focusedRow: callsPage.focusedIndex()

    function showFocusedCall(): void {
        const index = callsPage.focusedRow
        if (index >= 0)
            callsList.positionViewAtIndex(index, ListView.Center)
    }

    onFocusedCallIdChanged: {
        if (callsPage.focusedCallId !== "")
            callsPage.missedOnly = false
        Qt.callLater(callsPage.showFocusedCall)
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

    SectionHeader {
        text: qsTr("Recent Calls")
        FilterBar {
            objectName: "callsFilter"
            options: [
                { "key": "all", "label": qsTr("All"), "objectName": "callsFilterAll" },
                { "key": "missed", "label": qsTr("Missed"), "objectName": "callsFilterMissed" }
            ]
            current: callsPage.missedOnly ? "missed" : "all"
            onPicked: key => callsPage.missedOnly = key === "missed"
        }
        Controls.ToolButton {
            icon.name: "view-refresh"
            text: qsTr("Refresh from iPhone")
            display: Controls.AbstractButton.IconOnly
            enabled: !callsPage.bridge.busy
            Accessible.name: text
            Controls.ToolTip.text: text
            Controls.ToolTip.visible: hovered
            onClicked: callsPage.bridge.syncCallHistory()
        }
    }

    Controls.ScrollView {
        Layout.fillWidth: true
        Layout.fillHeight: true
        Controls.ScrollBar.horizontal.policy: Controls.ScrollBar.AlwaysOff

        ListView {
            id: callsList
            model: callsPage.visibleCalls()
            reuseItems: true
            onCountChanged: Qt.callLater(callsPage.showFocusedCall)
            section.property: "day"
            section.criteria: ViewSection.FullString
            section.delegate: Kirigami.ListSectionHeader {
                required property string section
                width: ListView.view.width
                text: section
            }

            EmptyState {
                anchors.centerIn: parent
                visible: callsList.count === 0
                icon.name: callsPage.bridge.callHistoryError !== "" ? "dialog-warning" : "call-start"
                text: callsPage.bridge.callHistoryError !== ""
                    ? qsTr("Call history is unavailable")
                    : (callsPage.missedOnly ? qsTr("No missed calls") : qsTr("No recent calls"))
                explanation: callsPage.bridge.callHistoryError
            }

            delegate: ListRow {
                id: callRow
                required property var modelData
                required property int index
                partPrefix: "call"
                density: "compact"
                highlighted: callRow.index === callsPage.focusedRow
                Accessible.name: callsPage.directionText(callRow.modelData.direction)
                    + ", " + callRow.modelData.caller + ", " + callRow.modelData.clock
                title: callRow.modelData.count > 1
                    ? callRow.modelData.caller + " (" + callRow.modelData.count + ")"
                    : callRow.modelData.caller
                titleColor: callRow.modelData.missed ? Kirigami.Theme.negativeTextColor
                                                     : Kirigami.Theme.textColor
                subtitle: callRow.modelData.known && callRow.modelData.address !== ""
                    ? callsPage.directionText(callRow.modelData.direction) + " · "
                        + callRow.modelData.address
                    : callsPage.directionText(callRow.modelData.direction)
                meta: callRow.modelData.clock
                leading: Component {
                    ContactAvatar {
                        bridge: callsPage.bridge
                        address: callRow.modelData.address || ""
                    }
                }
                badgeIcon: callsPage.directionIcon(callRow.modelData.direction)
                badgeColor: callRow.modelData.missed ? Kirigami.Theme.negativeTextColor
                                                     : Kirigami.Theme.textColor

                Controls.ToolButton {
                    objectName: "callBackButton"
                    visible: callRow.modelData.address !== ""
                    icon.name: "call-start"
                    text: qsTr("Call back")
                    display: Controls.AbstractButton.IconOnly
                    enabled: callsPage.canCall && !callsPage.bridge.busy
                    Controls.ToolTip.text: callsPage.canCall ? text
                        : qsTr("Calls are not ready.")
                    Controls.ToolTip.visible: hovered
                    onClicked: callsPage.callRequested(callRow.modelData.address)
                }
                Controls.ToolButton {
                    objectName: "callMessageButton"
                    visible: callRow.modelData.address !== ""
                    icon.name: "dialog-messages"
                    text: qsTr("Send a message")
                    display: Controls.AbstractButton.IconOnly
                    Controls.ToolTip.text: text
                    Controls.ToolTip.visible: hovered
                    onClicked: callsPage.messageRequested(callRow.modelData.address)
                }
            }
        }
    }
}
