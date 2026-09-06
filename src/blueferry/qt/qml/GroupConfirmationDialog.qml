pragma ComponentBehavior: Bound

import QtQuick
import org.kde.kirigami as Kirigami

Kirigami.PromptDialog {
    id: dialog
    required property var bridge
    property string threadKey: ""
    property string draft: ""
    title: qsTr("Confirm Group Recipients")
    standardButtons: Kirigami.Dialog.Cancel
    customFooterActions: [Kirigami.Action {
        text: qsTr("Send to These Recipients")
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
            dialog.subtitle = "<span>" + roster.replace(/&/g, "&amp;").replace(/</g, "&lt;")
                    .replace(/>/g, "&gt;").replace(/\n/g, "<br>") + "</span>"
            dialog.open()
        }
    }
}
