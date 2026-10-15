pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import "ui"

// Recent iPhone app notifications (opt-in, kept only in the daemon's
// memory). Main asks the bridge to fetch them only while this tab is shown.
// Every field comes from the iPhone and is rendered as plain text.
ColumnLayout {
    id: tab
    objectName: "notificationsTab"
    required property var bridge
    readonly property var info: tab.bridge.notificationsInfo || ({})
    readonly property string optInHint: (tab.bridge.featureHints || {}).notifications || ""
    readonly property var rows: tab.bridge.notifications || []
    spacing: 0

    SectionHeader {
        text: qsTr("Notifications")
        Controls.ToolButton {
            objectName: "notificationsRefresh"
            icon.name: "view-refresh"
            text: qsTr("Refresh")
            display: Controls.AbstractButton.IconOnly
            enabled: tab.optInHint === ""
            Accessible.name: text
            Controls.ToolTip.text: text
            Controls.ToolTip.visible: hovered
            onClicked: tab.bridge.refreshNotifications()
        }
    }

    Kirigami.InlineMessage {
        objectName: "notificationContentHint"
        Layout.fillWidth: true
        Layout.leftMargin: Kirigami.Units.largeSpacing
        Layout.rightMargin: Kirigami.Units.largeSpacing
        Layout.bottomMargin: Kirigami.Units.smallSpacing
        visible: tab.info.enabled === true && tab.info.content !== true
        type: Kirigami.MessageType.Information
        text: qsTr("Only app names and times are shown. Set BLUEFERRY_SHOW_NOTIFICATION_CONTENT=true in local.env to see titles and text.")
    }

    Controls.ScrollView {
        Layout.fillWidth: true
        Layout.fillHeight: true
        Controls.ScrollBar.horizontal.policy: Controls.ScrollBar.AlwaysOff

        ListView {
            id: notificationList
            model: tab.rows
            clip: true

            delegate: ListRow {
                id: row
                required property var modelData
                partPrefix: "notification"
                iconName: "preferences-desktop-notification"
                title: row.modelData.app
                bold: true
                meta: row.modelData.time
                subtitle: [row.modelData.title, row.modelData.subtitle || ""]
                    .filter(part => part !== "").join(" · ")
                body: row.modelData.body
                Accessible.name: row.modelData.app + ", " + row.modelData.time
            }

            EmptyState {
                objectName: "notificationsPlaceholder"
                anchors.centerIn: parent
                visible: notificationList.count === 0
                dimmed: tab.optInHint !== ""
                icon.name: "notifications"
                text: tab.optInHint !== ""
                    ? qsTr("Notification List Is Off")
                    : tab.info.error
                        ? qsTr("Notifications Are Unavailable")
                        : qsTr("No Notifications Yet")
                explanation: tab.optInHint !== ""
                    ? tab.optInHint
                    : tab.info.error || qsTr("iPhone app notifications appear here while BlueFerry is running. The list is not saved.")
            }
        }
    }
}
