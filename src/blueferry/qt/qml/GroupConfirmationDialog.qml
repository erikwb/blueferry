pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import "ui"

// Asks once before a message goes to a group's saved participant list. The
// roster comes from the backend as lines of plain text; every recipient is
// one row with an avatar, never markup.
Kirigami.Dialog {
    id: dialog
    required property var bridge
    property string threadKey: ""
    property string draft: ""
    property var recipients: []
    // The roster as escaped text (screen readers, older callers).
    readonly property string subtitle: "<span>" + dialog.recipients.map(line => line
            .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"))
        .join("<br>") + "</span>"
    title: qsTr("Confirm Group Recipients")
    preferredWidth: Kirigami.Units.gridUnit * 24
    standardButtons: Kirigami.Dialog.Cancel
    customFooterActions: [Kirigami.Action {
        text: qsTr("Send to These Recipients")
        icon.name: "document-send"
        onTriggered: {
            dialog.bridge.sendThread(dialog.threadKey, dialog.draft, true)
            dialog.close()
        }
    }]
    Connections {
        target: dialog.bridge
        function onGroupConfirmationRequested(key: string, body: string, roster: string): void {
            dialog.threadKey = key
            dialog.draft = body
            dialog.recipients = roster.split("\n").filter(line => line.trim() !== "")
            dialog.open()
        }
    }

    ColumnLayout {
        spacing: Kirigami.Units.smallSpacing
        Controls.Label {
            Layout.fillWidth: true
            Layout.leftMargin: Kirigami.Units.largeSpacing
            Layout.rightMargin: Kirigami.Units.largeSpacing
            text: qsTr("This message goes to everyone in BlueFerry's participant list:")
            wrapMode: Text.Wrap
        }
        Repeater {
            model: dialog.recipients
            delegate: ListRow {
                required property string modelData
                Layout.fillWidth: true
                density: "compact"
                iconName: "user-identity"
                title: modelData
                focusPolicy: Qt.NoFocus
                hoverEnabled: false
            }
        }
    }
}
