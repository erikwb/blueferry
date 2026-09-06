pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

Kirigami.Dialog {
    id: dialog
    required property var bridge
    property alias recipient: newRecipient.text
    property alias body: newMessageBody.text
    title: qsTr("New Message")
    preferredWidth: Kirigami.Units.gridUnit * 24
    standardButtons: Kirigami.Dialog.Cancel
    customFooterActions: [Kirigami.Action {
        id: newMessageSendAction
        text: qsTr("Send")
        icon.name: "document-send"
        enabled: newRecipient.text.trim() !== ""
            && newMessageBody.text.trim() !== "" && !dialog.bridge.busy
        onTriggered: {
            dialog.bridge.sendMessage(newRecipient.text, newMessageBody.text)
        }
    }]

    Connections {
        target: dialog.bridge
        function onMessageSendSucceeded(recipient: string, body: string): void {
            if (newRecipient.text === recipient && newMessageBody.text === body) {
                newRecipient.clear()
                newMessageBody.clear()
                dialog.close()
            }
        }
    }

    onOpened: {
        dialog.bridge.findContacts("")
        newRecipient.forceActiveFocus()
    }

    Timer {
        id: contactSearchTimer
        interval: 180
        onTriggered: dialog.bridge.findContacts(newRecipient.text)
    }

    ColumnLayout {
        spacing: Kirigami.Units.smallSpacing

        Controls.Label {
            text: qsTr("To")
            font.bold: true
        }
        Controls.TextField {
            id: newRecipient
            Layout.fillWidth: true
            placeholderText: qsTr("Contact, phone number, or email address")
            Accessible.name: qsTr("Recipient")
            onTextEdited: contactSearchTimer.restart()
        }
        ListView {
            id: contactResults
            Layout.fillWidth: true
            Layout.preferredHeight: count > 0
                ? Math.min(contentHeight, Kirigami.Units.gridUnit * 10) : 0
            visible: count > 0
            clip: true
            model: dialog.bridge.contactResults

            delegate: Controls.ItemDelegate {
                id: contactDelegate
                required property var modelData
                width: contactResults.width
                text: modelData.name + "\n" + (
                    modelData.address.indexOf("@") >= 0
                        ? modelData.address : "+" + modelData.address
                )
                contentItem: Controls.Label {
                    text: contactDelegate.text
                    textFormat: Text.PlainText
                    elide: Text.ElideRight
                }
                onClicked: {
                    newRecipient.text = modelData.address
                    dialog.bridge.findContacts("")
                    newMessageBody.forceActiveFocus()
                }
            }
        }
        Controls.Label {
            text: qsTr("Message")
            font.bold: true
        }
        ExpandingMessageComposer {
            id: newMessageBody
            placeholderText: qsTr("Write a Message")
            Accessible.name: qsTr("Message Text")
            onAccepted: {
                if (newMessageSendAction.enabled)
                    newMessageSendAction.trigger()
            }
        }
    }
}
