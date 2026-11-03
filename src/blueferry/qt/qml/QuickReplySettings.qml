pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

// The replies offered when declining a call with a message
// (Calls1.SetCallQuickReplies). Wording, the list and the checks come from
// quick_reply_view through the bridge; loaded only for daemons that report
// calls_quick_replies. Replies are the user's own texts: plain text only.
ColumnLayout {
    id: section
    objectName: "quickReplySettings"
    required property var bridge
    readonly property var status: section.bridge.status || ({})
    readonly property var view: section.bridge.quickReplySettingsView(section.status) || ({})
    readonly property var replies: section.view.replies || []
    readonly property bool canEdit: section.status.daemon === true && !section.bridge.busy
    // The reply being edited (-1: none) and its draft; kept here because the
    // rows are rebuilt whenever the status changes.
    property int editingIndex: -1
    property string editDraft: ""
    readonly property string editError: section.editingIndex >= 0
        ? section.bridge.quickReplyError(section.editDraft, section.editingIndex) : ""
    readonly property string addError: addField.text !== ""
        ? section.bridge.quickReplyError(addField.text, -1) : ""
    // quick_reply_view.MAX_REPLY_CHARS, via settings_view.
    readonly property int maxChars: section.view.max_chars || 32767
    // The save on its way ("add", "edit", "remove" or ""): the typed text
    // stays until the daemon saved it, and a failure keeps it for a retry.
    property string pendingSave: ""
    property string saveError: ""
    spacing: Kirigami.Units.smallSpacing

    function startEdit(index: int): void {
        section.editDraft = section.replies[index] || ""
        section.editingIndex = index
    }

    function saveEdit(): void {
        if (section.editingIndex < 0)
            return
        section.saveError = ""
        if (section.bridge.replaceQuickReply(section.editingIndex, section.editDraft) === "")
            section.pendingSave = "edit"
    }

    function add(): void {
        section.saveError = ""
        if (section.bridge.addQuickReply(addField.text) === "")
            section.pendingSave = "add"
    }

    function remove(index: int): void {
        section.saveError = ""
        section.pendingSave = "remove"
        section.bridge.removeQuickReply(index)
    }

    Connections {
        target: section.bridge

        function onQuickRepliesSaved() {
            if (section.pendingSave === "add")
                addField.text = ""
            else if (section.pendingSave === "edit")
                section.editingIndex = -1
            section.pendingSave = ""
        }

        function onQuickRepliesSaveFailed(message) {
            if (section.pendingSave !== "")
                section.saveError = String(message || "")
            section.pendingSave = ""
        }
    }

    // A saved list that changed under an open editor: stop editing a row
    // that no longer exists.
    onRepliesChanged: {
        if (section.editingIndex >= section.replies.length)
            section.editingIndex = -1
    }

    Controls.Label {
        Layout.fillWidth: true
        text: section.view.label || ""
        textFormat: Text.PlainText
        font.bold: true
    }
    Controls.Label {
        Layout.fillWidth: true
        text: section.view.hint || ""
        textFormat: Text.PlainText
        wrapMode: Text.WordWrap
        opacity: 0.7
    }
    Controls.Label {
        objectName: "quickReplyEmpty"
        Layout.fillWidth: true
        visible: section.replies.length === 0
        text: section.view.empty || ""
        textFormat: Text.PlainText
        wrapMode: Text.WordWrap
        font.italic: true
    }

    Repeater {
        model: section.replies
        delegate: ColumnLayout {
            id: row
            required property var modelData
            required property int index
            readonly property bool editing: section.editingIndex === row.index
            Layout.fillWidth: true
            spacing: 0
            RowLayout {
                Layout.fillWidth: true
                Controls.Label {
                    objectName: "quickReplyText" + row.index
                    Layout.fillWidth: true
                    visible: !row.editing
                    text: String(row.modelData)
                    textFormat: Text.PlainText
                    wrapMode: Text.WordWrap
                }
                Controls.TextField {
                    objectName: "quickReplyEditField" + row.index
                    Layout.fillWidth: true
                    visible: row.editing
                    text: section.editDraft
                    maximumLength: section.maxChars
                    Accessible.name: qsTr("Edit reply")
                    onTextEdited: section.editDraft = text
                    onAccepted: section.saveEdit()
                    onVisibleChanged: {
                        if (visible)
                            forceActiveFocus()
                    }
                }
                Controls.ToolButton {
                    objectName: "quickReplySave" + row.index
                    visible: row.editing
                    icon.name: "dialog-ok-apply"
                    text: qsTr("Save")
                    display: Controls.AbstractButton.IconOnly
                    enabled: section.canEdit && section.editError === ""
                    Accessible.name: text
                    Controls.ToolTip.text: text
                    Controls.ToolTip.visible: hovered
                    onClicked: section.saveEdit()
                }
                Controls.ToolButton {
                    visible: row.editing
                    icon.name: "dialog-cancel"
                    text: qsTr("Cancel")
                    display: Controls.AbstractButton.IconOnly
                    Accessible.name: text
                    Controls.ToolTip.text: text
                    Controls.ToolTip.visible: hovered
                    onClicked: section.editingIndex = -1
                }
                Controls.ToolButton {
                    objectName: "quickReplyEdit" + row.index
                    visible: !row.editing
                    icon.name: "document-edit"
                    text: qsTr("Edit")
                    display: Controls.AbstractButton.IconOnly
                    enabled: section.canEdit
                    Accessible.name: text
                    Controls.ToolTip.text: text
                    Controls.ToolTip.visible: hovered
                    onClicked: section.startEdit(row.index)
                }
                Controls.ToolButton {
                    objectName: "quickReplyRemove" + row.index
                    visible: !row.editing
                    icon.name: "list-remove"
                    text: qsTr("Remove")
                    display: Controls.AbstractButton.IconOnly
                    enabled: section.canEdit
                    Accessible.name: text
                    Controls.ToolTip.text: text
                    Controls.ToolTip.visible: hovered
                    onClicked: section.remove(row.index)
                }
            }
            Controls.Label {
                objectName: "quickReplyEditError" + row.index
                Layout.fillWidth: true
                visible: row.editing && text !== ""
                text: row.editing ? section.editError : ""
                textFormat: Text.PlainText
                wrapMode: Text.WordWrap
                color: Kirigami.Theme.negativeTextColor
            }
        }
    }

    RowLayout {
        Layout.fillWidth: true
        visible: section.view.can_add === true
        Controls.TextField {
            id: addField
            objectName: "quickReplyAddField"
            Layout.fillWidth: true
            placeholderText: section.view.add_label || ""
            Accessible.name: placeholderText
            maximumLength: section.maxChars
            onAccepted: {
                if (section.addError === "" && text !== "")
                    section.add()
            }
        }
        Controls.Button {
            objectName: "quickReplyAdd"
            text: section.view.add_label || ""
            icon.name: "list-add"
            enabled: section.canEdit && addField.text !== "" && section.addError === ""
            onClicked: section.add()
        }
    }
    Controls.Label {
        objectName: "quickReplyAddError"
        Layout.fillWidth: true
        visible: text !== ""
        text: section.view.can_add === true ? section.addError : ""
        textFormat: Text.PlainText
        wrapMode: Text.WordWrap
        color: Kirigami.Theme.negativeTextColor
    }
    // A save the daemon refused; the typed text is still there.
    Controls.Label {
        objectName: "quickReplySaveError"
        Layout.fillWidth: true
        visible: text !== ""
        text: section.saveError
        textFormat: Text.PlainText
        wrapMode: Text.WordWrap
        color: Kirigami.Theme.negativeTextColor
    }
}
