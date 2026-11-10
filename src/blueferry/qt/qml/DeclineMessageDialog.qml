pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

// Decline a ringing call and text the caller (Calls1.DeclineWithMessage):
// one button per saved reply, then a field for an own text. Wording, the
// choices and the length limit come from quick_reply_view.dialog_view through
// bridge.declineMessageView; the replies are the user's own texts and are
// shown as plain text only.
Kirigami.Dialog {
    id: dialog
    objectName: "declineMessageDialog"
    required property var bridge
    property string callId: ""
    property var view: ({})
    readonly property string customValue: dialog.view.custom || ""
    readonly property var replies: (dialog.view.choices || []).filter(
        choice => choice.value !== dialog.customValue)
    readonly property var customChoice: (dialog.view.choices || []).find(
        choice => choice.value === dialog.customValue) || ({})
    readonly property string customError: dialog.bridge.declineMessageError(customField.text)

    function present(callId: string, view: var): void {
        dialog.callId = callId
        dialog.view = view || ({})
        customField.text = ""
        dialog.open()
    }

    function send(text: string): void {
        if (dialog.callId !== "" && dialog.bridge.declineMessageError(text) === "")
            dialog.bridge.declineCallWithMessage(dialog.callId, text)
        dialog.close()
    }

    title: dialog.view.title || ""
    preferredWidth: Kirigami.Units.gridUnit * 22
    padding: Kirigami.Units.largeSpacing
    standardButtons: Kirigami.Dialog.Cancel
    onClosed: dialog.callId = ""

    // The call was answered, declined elsewhere or ended: nothing to decline.
    Connections {
        target: dialog.bridge

        function onPhoneCallsChanged() {
            if (!dialog.visible)
                return
            const calls = dialog.bridge.phoneCalls || []
            const ringing = calls.some(call => call.call_id === dialog.callId
                && call.ringing === true)
            if (!ringing)
                dialog.close()
        }
    }

    ColumnLayout {
        spacing: Kirigami.Units.smallSpacing

        Repeater {
            model: dialog.replies
            delegate: Controls.Button {
                id: reply
                required property var modelData
                required property int index
                objectName: "declineReply" + reply.index
                Layout.fillWidth: true
                // Plain text: a saved reply is user data, never markup.
                contentItem: Controls.Label {
                    text: reply.modelData.label || ""
                    textFormat: Text.PlainText
                    wrapMode: Text.WordWrap
                    horizontalAlignment: Text.AlignLeft
                }
                Accessible.name: reply.modelData.label || ""
                enabled: !dialog.bridge.busy
                onClicked: dialog.send(reply.modelData.value)
            }
        }

        Controls.Label {
            Layout.fillWidth: true
            Layout.topMargin: dialog.replies.length > 0 ? Kirigami.Units.smallSpacing : 0
            text: dialog.customChoice.label || ""
            textFormat: Text.PlainText
        }
        RowLayout {
            Layout.fillWidth: true
            Controls.TextField {
                id: customField
                objectName: "declineCustomField"
                Layout.fillWidth: true
                placeholderText: dialog.view.placeholder || ""
                Accessible.name: placeholderText
                // quick_reply_view.MAX_CUSTOM_CHARS, via dialog_view.
                maximumLength: dialog.view.max_chars || 32767
                onAccepted: {
                    if (dialog.customError === "")
                        dialog.send(customField.text)
                }
            }
            Controls.Button {
                objectName: "declineCustomSend"
                text: qsTr("Send")
                icon.name: "document-send"
                enabled: dialog.customError === "" && !dialog.bridge.busy
                Controls.ToolTip.text: dialog.customError
                Controls.ToolTip.visible: hovered && dialog.customError !== ""
                onClicked: dialog.send(customField.text)
            }
        }
    }
}
