import QtQuick
import org.kde.kirigami as Kirigami

// The latest message in a one-to-one conversation may have been sent in a
// group; a reply goes to its sender only after the user says so. Names and
// addresses come from the phone, so only the escaped subtitle shows them.
Kirigami.PromptDialog {
    id: dialog
    required property var bridge
    property string threadKey: ""
    property string draft: ""
    property string senderName: ""
    property string address: ""
    title: qsTr("Reply to the Sender Only?")
    subtitle: "<span>" + qsTr("The latest message from %1 may have been sent in a group. Your reply goes only to %2.")
        .arg(dialog.escaped(dialog.senderName)).arg(dialog.escaped(dialog.address)) + "</span>"
    standardButtons: Kirigami.Dialog.Cancel
    customFooterActions: [Kirigami.Action {
        text: qsTr("Reply Only to Sender")
        icon.name: "document-send"
        onTriggered: {
            dialog.bridge.sendThread(dialog.threadKey, dialog.draft, true)
            dialog.close()
        }
    }]

    function escaped(value: string): string {
        return value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    }

    Connections {
        target: dialog.bridge
        function onSenderConfirmationRequested(key: string, body: string, name: string, address: string): void {
            dialog.threadKey = key
            dialog.draft = body
            dialog.senderName = name
            dialog.address = address
            dialog.open()
        }
    }
}
