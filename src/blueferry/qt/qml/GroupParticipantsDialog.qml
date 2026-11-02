pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import "ui"

// Who is in a group BlueFerry cannot route on its own. A reply held back
// for the members (groupParticipantsRequested) is sent once they are saved.
Kirigami.Dialog {
    id: dialog
    required property var bridge
    property var thread: null
    // The reply waiting for these members; empty when only editing them.
    property string draft: ""
    property var entries: []
    readonly property var missing: logic.missingMembers(dialog.thread, dialog.entries)
    // The query the current contact results answer. Results for an earlier
    // query are never offered while the debounced search is pending.
    property string searchedText: ""
    readonly property var suggestions: addField.text.trim() === ""
        || addField.text !== dialog.searchedText
        ? [] : logic.participantSuggestions(dialog.bridge.contactResults, dialog.entries)
    title: thread ? qsTr("Who is in %1?").arg(thread.name) : ""
    preferredWidth: Kirigami.Units.gridUnit * 26
    standardButtons: Kirigami.Dialog.Cancel

    function request(selected: var, pendingDraft: string): void {
        dialog.thread = selected
        dialog.draft = pendingDraft
        dialog.open()
    }

    function add(value: string, name: string): bool {
        const next = logic.addParticipant(dialog.entries, value, name)
        if (next === null) return false
        dialog.entries = next
        addField.clear()
        dialog.bridge.findContacts("")
        return true
    }

    customFooterActions: [Kirigami.Action {
        text: qsTr("Save Participants")
        icon.name: "document-save"
        enabled: dialog.thread !== null && dialog.entries.length >= 2
            && dialog.entries.length <= logic.maxParticipants && !dialog.bridge.busy
        onTriggered: {
            dialog.bridge.setGroupParticipants(
                dialog.thread.key, dialog.entries.map(entry => entry.address), dialog.draft
            )
            dialog.close()
        }
    }]

    Connections {
        target: dialog.bridge
        function onGroupParticipantsRequested(key: string, body: string): void {
            const selected = logic.threadByKey(dialog.bridge.threads, key)
            if (selected) dialog.request(selected, body)
        }
    }

    onAboutToShow: {
        dialog.entries = logic.participantDraft(dialog.thread)
        addField.clear()
        dialog.bridge.findContacts("")
    }
    onOpened: addField.forceActiveFocus()
    // Cancel leaves the draft in the composer and sends nothing.
    onClosed: dialog.draft = ""

    ConversationLogic { id: logic }

    Timer {
        id: searchTimer
        interval: 180
        onTriggered: {
            dialog.searchedText = addField.text
            dialog.bridge.findContacts(addField.text)
        }
    }

    ColumnLayout {
        spacing: Kirigami.Units.smallSpacing

        Controls.Label {
            Layout.fillWidth: true
            text: !dialog.thread ? ""
                : dialog.thread.group_origin === "unnamed"
                    ? qsTr("BlueFerry couldn't match everyone in this group to one phone number or email. Add everyone else in it so you can reply from here.")
                    : qsTr("BlueFerry can't tell who is in %1. Add everyone else in it so you can reply from here.")
                        .arg(dialog.thread.name)
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
        }
        Controls.Label {
            Layout.fillWidth: true
            text: qsTr("This changes only BlueFerry, not the group on your iPhone.")
            wrapMode: Text.Wrap
            font: Kirigami.Theme.smallFont
            color: Kirigami.Theme.disabledTextColor
        }
        Repeater {
            model: dialog.entries
            delegate: ListRow {
                id: entryRow
                required property var modelData
                Layout.fillWidth: true
                density: "compact"
                iconName: "user-identity"
                title: entryRow.modelData.name || entryRow.modelData.address
                subtitle: entryRow.modelData.name ? entryRow.modelData.address : ""
                focusPolicy: Qt.NoFocus
                pinActions: true
                Controls.ToolButton {
                    icon.name: "list-remove"
                    text: qsTr("Remove %1").arg(entryRow.title)
                    display: Controls.AbstractButton.IconOnly
                    Accessible.name: text
                    Controls.ToolTip.text: text
                    Controls.ToolTip.visible: hovered
                    onClicked: dialog.entries = logic.removeParticipant(
                        dialog.entries, entryRow.modelData.address
                    )
                }
            }
        }
        Controls.Label {
            objectName: "missingMembersHint"
            Layout.fillWidth: true
            visible: dialog.missing.length > 0
            text: qsTr("Still needed: %1").arg(dialog.missing.join(", "))
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            color: Kirigami.Theme.neutralTextColor
        }
        Controls.TextField {
            id: addField
            objectName: "participantSearch"
            Layout.fillWidth: true
            placeholderText: qsTr("Add a contact, phone number, or email")
            Accessible.name: qsTr("Add Participant")
            onTextEdited: searchTimer.restart()
            // A typed address first; otherwise the best contact match.
            onAccepted: {
                if (!dialog.add(text, "") && dialog.suggestions.length > 0)
                    dialog.add(dialog.suggestions[0].address, dialog.suggestions[0].name)
            }
        }
        ListView {
            id: suggestionList
            objectName: "participantSuggestions"
            Layout.fillWidth: true
            Layout.preferredHeight: count > 0
                ? Math.min(contentHeight, Kirigami.Units.gridUnit * 8) : 0
            visible: count > 0
            clip: true
            model: dialog.suggestions

            delegate: ListRow {
                id: suggestionRow
                required property var modelData
                density: "compact"
                title: suggestionRow.modelData.name
                subtitle: suggestionRow.modelData.address
                leading: Component {
                    ContactAvatar {
                        bridge: dialog.bridge
                        address: suggestionRow.modelData.address
                    }
                }
                onClicked: dialog.add(suggestionRow.modelData.address, suggestionRow.modelData.name)
            }
        }
        Kirigami.InlineMessage {
            Layout.fillWidth: true
            visible: dialog.thread !== null && dialog.thread.group_origin !== "unnamed"
            type: Kirigami.MessageType.Warning
            text: qsTr("Groups with the same name share this list.")
        }
    }
}
