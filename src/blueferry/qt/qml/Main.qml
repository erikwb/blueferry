pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

Kirigami.ApplicationWindow {
    id: root

    ConversationLogic { id: conversationLogic }

    required property var bridge
    property string selectedThreadKey: ""
    onSelectedThreadKeyChanged: root.markSelectedThreadRead()
    onActiveChanged: root.markSelectedThreadRead()
    onIphoneSettingsPageChanged: Qt.callLater(root.markSelectedThreadRead)
    property bool firstRunRedirected: false
    property var iphoneSettingsPage: null
    property string pendingMessageHandle: ""

    visible: true
    width: 980
    height: 680
    minimumWidth: 420
    minimumHeight: 480
    title: qsTr("BlueFerry")

    function threadByKey(key) {
        return conversationLogic.threadByKey(bridge.threads, key)
    }

    function selectedThread() {
        return threadByKey(selectedThreadKey)
    }

    function threadIsUnread(thread) {
        return conversationLogic.threadIsUnread(thread)
    }

    function markSelectedThreadRead() {
        if (!root.visible || !root.active || root.iphoneSettingsPage !== null)
            return
        const thread = selectedThread()
        if (thread && threadIsUnread(thread))
            bridge.markThreadRead(thread.key)
    }

    function selectMessage(handle) {
        const thread = conversationLogic.threadForMessage(bridge.threads, handle)
        if (!thread) return false
        closePhoneSettings()
        pageStack.currentIndex = 0
        selectedThreadKey = thread.key
        pendingMessageHandle = ""
        return true
    }

    function htmlEscape(value) {
        return String(value || "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
    }

    function escapedRichText(value) {
        // SelectableLabel uses AutoText. The wrapper makes escaped entities
        // render as text instead of appearing literally as "&amp;".
        return "<span>" + value + "</span>"
    }

    function mapConnectionRefused() {
        const status = bridge.status || ({})
        return status.map_connection_refused === true
    }

    function retainedStorageUnavailable() {
        const status = bridge.status || ({})
        return status.daemon === true
            && status.storage_policy !== undefined
            && status.storage_policy !== "none"
            && status.storage_state !== "ready"
    }

    function storageDetail() {
        const status = bridge.status || ({})
        if (status.storage_detail)
            return status.storage_detail
        return qsTr("Local conversation history is unavailable.")
    }

    function storageStatusText() {
        const status = bridge.status || ({})
        if (status.storage_policy === "none")
            return qsTr("Disabled")
        if (status.storage_state === "ready")
            return qsTr("Available")
        if (status.storage_state === "locked")
            return qsTr("Locked")
        return qsTr("Unavailable")
    }

    function openPhoneSettings() {
        // Utility pages belong in Kirigami's PageRow. Its modal layers are an
        // anchored StackView internally: animated pushes warn about those
        // anchors, while forcing an Immediate push can create an empty layer.
        if (iphoneSettingsPage !== null) {
            pageStack.currentIndex = pageStack.depth - 1
            return
        }
        iphoneSettingsPage = pageStack.push(iphonePageLoader.item)
    }

    function closePhoneSettings() {
        if (iphoneSettingsPage === null)
            return
        const page = iphoneSettingsPage
        iphoneSettingsPage = null
        pageStack.removePage(page)
    }

    function togglePhoneSettings() {
        if (iphoneSettingsPage !== null)
            closePhoneSettings()
        else
            openPhoneSettings()
    }

    function warnAboutRosterChanges() {
        const thread = conversationLogic.nextRosterWarning(bridge.threads)
        if (!thread) return
        rosterChangedDialog.thread = thread
        rosterChangedDialog.open()
    }

    Connections {
        target: root.bridge

        function onPairingConfirmationRequested(passkey) {
            pairingConfirmationDialog.passkey = passkey
            pairingConfirmationDialog.open()
        }

        function onThreadsChanged() {
            const selected = root.selectedThread()
            root.selectedThreadKey = selected ? selected.key : ""
            if (root.pendingMessageHandle !== "")
                root.selectMessage(root.pendingMessageHandle)
            root.warnAboutRosterChanges()
            root.markSelectedThreadRead()
        }

        function onMessageOpenRequested(handle) {
            root.pendingMessageHandle = handle
            if (!root.selectMessage(handle))
                root.bridge.refresh()
        }

        function onSetupLoadedChanged() {
            if (root.bridge.setupLoaded && !root.bridge.configured && !root.firstRunRedirected) {
                root.firstRunRedirected = true
                root.openPhoneSettings()
            }
        }

    }

    Shortcut {
        sequences: [StandardKey.Refresh]
        onActivated: root.bridge.refresh()
    }
    Shortcut {
        sequence: "Ctrl+Q"
        onActivated: Qt.quit()
    }
    Shortcut {
        sequence: "Ctrl+?"
        onActivated: shortcutsDialog.open()
    }

    pageStack.initialPage: messagesPage

    globalDrawer: Kirigami.GlobalDrawer {
        actions: [
            Kirigami.Action {
                text: qsTr("iPhone Settings")
                icon.name: "phone"
                onTriggered: root.openPhoneSettings()
            },
            Kirigami.Action {
                text: qsTr("Keyboard Shortcuts")
                icon.name: "preferences-desktop-keyboard-shortcuts"
                onTriggered: shortcutsDialog.open()
            },
            Kirigami.Action {
                text: qsTr("About BlueFerry")
                icon.name: "help-about"
                onTriggered: {
                    root.closePhoneSettings()
                    root.pageStack.layers.push(aboutPage)
                }
            },
            Kirigami.Action {
                text: qsTr("Quit")
                icon.name: "application-exit"
                shortcut: StandardKey.Quit
                onTriggered: Qt.quit()
            }
        ]
    }

    Kirigami.PromptDialog {
        id: rosterChangedDialog
        property var thread: null
        title: qsTr("Group Membership May Have Changed")
        subtitle: thread
            ? root.escapedRichText(
                qsTr("%1 sent a message to %2, but is not in BlueFerry's saved participant list. Replies are disabled until you review the list. This can also happen if you have multiple groups named %2, because BlueFerry cannot distinguish them.")
                    .arg(root.htmlEscape(thread.unexpected_sender || qsTr("Someone new")))
                    .arg(root.htmlEscape(thread.name))
              )
            : ""
        dialogType: Kirigami.PromptDialog.Warning
        standardButtons: Kirigami.Dialog.Cancel
        customFooterActions: [Kirigami.Action {
            text: qsTr("Review Participants")
            icon.name: "system-users"
            onTriggered: {
                const selected = rosterChangedDialog.thread
                rosterChangedDialog.close()
                groupParticipantsDialog.thread = selected
                groupParticipantsDialog.open()
            }
        }]
    }

    Kirigami.Dialog {
        id: groupParticipantsDialog
        property var thread: null
        title: thread ? qsTr("Who is in %1?").arg(thread.name) : ""
        preferredWidth: Kirigami.Units.gridUnit * 28
        standardButtons: Kirigami.Dialog.Cancel

        function recipients() {
            return conversationLogic.participantLines(groupParticipantEditor.text)
        }

        customFooterActions: [Kirigami.Action {
            text: qsTr("Save Participants")
            icon.name: "document-save"
            enabled: groupParticipantsDialog.recipients().length >= 2
                && !root.bridge.busy
            onTriggered: {
                root.bridge.setGroupParticipants(
                    groupParticipantsDialog.thread.key,
                    groupParticipantsDialog.recipients()
                )
                groupParticipantsDialog.close()
            }
        }]

        onOpened: {
            groupParticipantEditor.text = thread
                ? (thread.recipients || []).join("\n") : ""
            groupParticipantEditor.forceActiveFocus()
        }

        ColumnLayout {
            spacing: Kirigami.Units.smallSpacing

            Controls.Label {
                Layout.fillWidth: true
                text: groupParticipantsDialog.thread
                    ? qsTr("%1 has sent a message to a group named %2, which you're a member of. BlueFerry can't determine the participants of this group chat, but if you fill in the members, it can work.")
                        .arg(groupParticipantsDialog.thread.prompt_sender || qsTr("Someone"))
                        .arg(groupParticipantsDialog.thread.name)
                    : ""
                textFormat: Text.PlainText
                wrapMode: Text.Wrap
            }
            Controls.Label {
                Layout.fillWidth: true
                text: qsTr("Enter every other participant's phone number or Apple ID email, one per line.")
                wrapMode: Text.Wrap
            }
            Kirigami.InlineMessage {
                Layout.fillWidth: true
                visible: true
                type: Kirigami.MessageType.Information
                text: qsTr("Changing this list only updates BlueFerry's local understanding of the group. It does not add or remove anyone in Messages on your iPhone.")
            }
            Controls.TextArea {
                id: groupParticipantEditor
                Layout.fillWidth: true
                Layout.preferredHeight: Kirigami.Units.gridUnit * 7
                placeholderText: qsTr("One participant per line")
                wrapMode: TextEdit.NoWrap
                Accessible.name: qsTr("Group Participants")
            }
            Kirigami.InlineMessage {
                Layout.fillWidth: true
                visible: true
                type: Kirigami.MessageType.Warning
                text: groupParticipantsDialog.thread
                    ? root.escapedRichText(
                        qsTr("BlueFerry identifies named groups by name. If you have multiple groups named %1, BlueFerry may combine them and use the wrong participant list. This list can also become outdated if the group is renamed or its membership changes.")
                            .arg(root.htmlEscape(groupParticipantsDialog.thread.name))
                      )
                    : ""
            }
        }
    }

    Kirigami.PromptDialog {
        id: clearDialog
        title: qsTr("Clear Local History?")
        subtitle: qsTr("This deletes local message history and group metadata. Nothing is deleted from the iPhone.")
        dialogType: Kirigami.PromptDialog.Warning
        standardButtons: Kirigami.Dialog.Cancel
        customFooterActions: [Kirigami.Action {
            text: qsTr("Clear History")
            icon.name: "edit-clear-history"
            onTriggered: {
                root.bridge.clearHistory()
                clearDialog.close()
            }
        }]
    }

    Kirigami.PromptDialog {
        id: deleteThreadsDialog
        property string threadKey: ""
        title: qsTr("Delete Conversation?")
        subtitle: qsTr("This permanently deletes this local message history and group metadata. Nothing is deleted from your iPhone. A new message can create the conversation again.")
        dialogType: Kirigami.PromptDialog.Warning
        standardButtons: Kirigami.Dialog.Cancel
        customFooterActions: [Kirigami.Action {
            text: qsTr("Delete Locally")
            icon.name: "edit-delete"
            enabled: !root.bridge.busy
            onTriggered: {
                root.bridge.deleteThreads([deleteThreadsDialog.threadKey])
                deleteThreadsDialog.close()
            }
        }]
    }

    Controls.Menu {
        id: threadContextMenu
        property string threadKey: ""

        Controls.MenuItem {
            text: {
                const thread = root.threadByKey(threadContextMenu.threadKey)
                return thread && thread.starred
                    ? qsTr("Unstar Conversation") : qsTr("Star Conversation")
            }
            icon.name: {
                const thread = root.threadByKey(threadContextMenu.threadKey)
                return thread && thread.starred
                    ? "non-starred-symbolic" : "starred-symbolic"
            }
            onTriggered: {
                const thread = root.threadByKey(threadContextMenu.threadKey)
                root.bridge.setThreadStarred(
                    threadContextMenu.threadKey,
                    !(thread && thread.starred)
                )
            }
        }
        Controls.MenuItem {
            text: qsTr("Delete Conversation")
            icon.name: "edit-delete"
            onTriggered: {
                deleteThreadsDialog.threadKey = threadContextMenu.threadKey
                deleteThreadsDialog.open()
            }
        }
    }

    Kirigami.PromptDialog {
        id: storageChangeDialog
        property string requestedPolicy: ""
        title: requestedPolicy === "none"
            ? qsTr("Stop Retaining Local Data?")
            : requestedPolicy === "plaintext"
                ? qsTr("Store Local Data Without Encryption?")
                : qsTr("Use Encrypted Local Storage?")
        subtitle: requestedPolicy === "none"
            ? qsTr("This clears message history and cached contacts, then removes BlueFerry's storage key. Nothing on the iPhone is deleted.")
            : requestedPolicy === "plaintext"
                ? qsTr("This clears existing local message history and cached contacts. New local data will be stored unencrypted and can be read by anyone with access to your files. Nothing on the iPhone is deleted.")
                : qsTr("Changing storage protection clears existing local message history and cached contacts. New local data will be encrypted with your desktop keyring. Nothing on the iPhone is deleted.")
        dialogType: Kirigami.PromptDialog.Warning
        standardButtons: Kirigami.Dialog.Cancel
        customFooterActions: [Kirigami.Action {
            text: storageChangeDialog.requestedPolicy === "none"
                ? qsTr("Clear and Stop Retaining")
                : storageChangeDialog.requestedPolicy === "plaintext"
                    ? qsTr("Clear and Store Unencrypted")
                    : qsTr("Clear and Use Encryption")
            icon.name: "edit-delete"
            onTriggered: {
                root.bridge.setStoragePolicy(storageChangeDialog.requestedPolicy)
                storageChangeDialog.close()
            }
        }]
        onClosed: root.bridge.refresh()
    }

    Kirigami.PromptDialog {
        id: restartBluetoothDialog
        title: qsTr("Restart Bluetooth?")
        subtitle: qsTr("Bluetooth devices will disconnect briefly. Polkit may request authentication.")
        dialogType: Kirigami.PromptDialog.Warning
        standardButtons: Kirigami.Dialog.Cancel
        customFooterActions: [Kirigami.Action {
            text: qsTr("Restart Bluetooth")
            icon.name: "network-bluetooth"
            onTriggered: {
                root.bridge.activateBluetooth()
                restartBluetoothDialog.close()
            }
        }]
    }

    Kirigami.PromptDialog {
        id: pairingConfirmationDialog
        property string passkey: ""
        title: passkey === "" ? qsTr("Approve Bluetooth Pairing?")
            : qsTr("Do the Bluetooth Codes Match?")
        subtitle: passkey === ""
            ? qsTr("Approve only if you started this pairing from BlueFerry.")
            : qsTr("Confirm that %1 is shown on both this computer and the iPhone.").arg(passkey)
        dialogType: Kirigami.PromptDialog.Information
        standardButtons: Kirigami.Dialog.NoButton
        closePolicy: Controls.Popup.NoAutoClose
        customFooterActions: [
            Kirigami.Action {
                text: qsTr("Cancel Pairing")
                onTriggered: {
                    root.bridge.answerPairingConfirmation(false)
                    pairingConfirmationDialog.close()
                }
            },
            Kirigami.Action {
                text: pairingConfirmationDialog.passkey === ""
                    ? qsTr("Approve Pairing") : qsTr("Codes Match")
                icon.name: "dialog-ok-apply"
                onTriggered: {
                    root.bridge.answerPairingConfirmation(true)
                    pairingConfirmationDialog.close()
                }
            }
        ]
    }

    Kirigami.PromptDialog {
        id: forgetDialog
        property string mac: ""
        title: qsTr("Unpair This iPhone?")
        subtitle: qsTr("Also forget this computer in the iPhone Bluetooth settings before pairing again.")
        dialogType: Kirigami.PromptDialog.Warning
        standardButtons: Kirigami.Dialog.Cancel
        customFooterActions: [Kirigami.Action {
            text: qsTr("Unpair")
            icon.name: "edit-delete-remove"
            onTriggered: {
                root.bridge.forgetDevice(forgetDialog.mac)
                forgetDialog.close()
            }
        }]
    }

    Kirigami.PromptDialog {
        id: pairingIssueDialog
        title: qsTr("Report Pairing Issue")
        subtitle: qsTr("A pairing report was saved at %1. Attach that file to a GitHub issue and include the iPhone model and iOS version.").arg(root.bridge.pairingIssueReport)
        standardButtons: Kirigami.Dialog.Cancel
        customFooterActions: [Kirigami.Action {
            text: qsTr("Open GitHub")
            icon.name: "internet-web-browser"
            onTriggered: {
                root.bridge.filePairingIssue()
                pairingIssueDialog.close()
            }
        }]
    }

    Kirigami.PromptDialog {
        id: replaceTargetDialog
        property string mac: ""
        property bool compatibilityMode: false
        property bool explicitPairing: false
        title: qsTr("Replace the Saved iPhone?")
        subtitle: qsTr("Pairing this iPhone will remove BlueFerry's saved phone and its local Bluetooth bond. Before continuing, also forget this computer in the old iPhone's Bluetooth settings.")
        dialogType: Kirigami.PromptDialog.Warning
        standardButtons: Kirigami.Dialog.Cancel
        customFooterActions: [Kirigami.Action {
            text: qsTr("Replace and Pair")
            icon.name: "edit-delete-remove"
            onTriggered: {
                root.bridge.replaceAndPair(
                    root.bridge.configuredMac,
                    replaceTargetDialog.mac,
                    replaceTargetDialog.compatibilityMode,
                    replaceTargetDialog.explicitPairing
                )
                replaceTargetDialog.close()
            }
        }]
    }

    Kirigami.PromptDialog {
        id: shortcutsDialog
        title: qsTr("Keyboard Shortcuts")
        subtitle: qsTr("Refresh — Ctrl+R\nQuit — Ctrl+Q\nKeyboard Shortcuts — Ctrl+?")
        standardButtons: Kirigami.Dialog.Close
    }

    GroupConfirmationDialog {
        id: confirmGroupDialog
        bridge: root.bridge
    }

    NewMessageDialog {
        id: newMessageDialog
        bridge: root.bridge
    }

    Kirigami.Page {
        id: messagesPage
        visible: false
        title: qsTr("Messages")
        padding: 0
        property bool narrow: width < 680
        property var thread: root.selectedThread()

        ColumnLayout {
            anchors.fill: parent
            spacing: 0

                Kirigami.InlineMessage {
                    Layout.fillWidth: true
                    visible: root.bridge.errorText !== ""
                    text: root.htmlEscape(root.bridge.errorText)
                    type: Kirigami.MessageType.Error
                    position: Kirigami.InlineMessage.Position.Header
                    actions: [
                        Kirigami.Action {
                            text: qsTr("Open iPhone Settings")
                            onTriggered: root.openPhoneSettings()
                        }
                    ]
                }

                Kirigami.InlineMessage {
                    Layout.fillWidth: true
                    visible: root.mapConnectionRefused()
                    text: qsTr("iPhone is refusing message connections; is it connected to another computer?")
                    type: Kirigami.MessageType.Warning
                    position: Kirigami.InlineMessage.Position.Header
                    actions: [
                        Kirigami.Action {
                            text: qsTr("Open iPhone Settings")
                            onTriggered: root.openPhoneSettings()
                        }
                    ]
                }

                Kirigami.InlineMessage {
                    Layout.fillWidth: true
                    visible: root.retainedStorageUnavailable()
                    text: root.htmlEscape(root.storageDetail())
                    type: Kirigami.MessageType.Warning
                    position: Kirigami.InlineMessage.Position.Header
                    actions: [
                        Kirigami.Action {
                            visible: root.bridge.status.storage_policy === "encrypted"
                            text: qsTr("Unlock Local Data")
                            icon.name: "document-decrypt"
                            enabled: !root.bridge.busy
                            onTriggered: root.bridge.unlockStorage()
                        },
                        Kirigami.Action {
                            text: qsTr("Open Settings")
                            onTriggered: root.openPhoneSettings()
                        }
                    ]
                }

                Controls.SplitView {
                    id: messagesSplit
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    orientation: Qt.Horizontal

                    handle: Item {
                        implicitWidth: messagesPage.narrow
                            ? 0 : Kirigami.Units.smallSpacing
                        visible: !messagesPage.narrow

                        Kirigami.Separator {
                            anchors.centerIn: parent
                            height: parent.height
                        }
                    }

                    ColumnLayout {
                        Controls.SplitView.fillWidth: messagesPage.narrow
                        Controls.SplitView.preferredWidth: messagesPage.narrow
                            ? messagesSplit.width : messagesSplit.width * 0.35
                        Controls.SplitView.minimumWidth: messagesPage.narrow
                            ? 0 : Kirigami.Units.gridUnit * 12
                        visible: !messagesPage.narrow || root.selectedThreadKey === ""
                        spacing: 0

                        Controls.ToolBar {
                            Layout.fillWidth: true

                            contentItem: RowLayout {
                                Controls.Label {
                                    Layout.fillWidth: true
                                    text: qsTr("Conversations")
                                    font.bold: true
                                    leftPadding: Kirigami.Units.smallSpacing
                                }
                                Controls.ToolButton {
                                    icon.name: "list-add"
                                    text: qsTr("New Message")
                                    display: Controls.AbstractButton.IconOnly
                                    Accessible.name: text
                                    Controls.ToolTip.text: text
                                    Controls.ToolTip.visible: hovered
                                    onClicked: newMessageDialog.open()
                                }
                                Controls.ToolButton {
                                    visible: messagesPage.narrow
                                    icon.name: "settings-configure"
                                    text: qsTr("Settings")
                                    display: Controls.AbstractButton.IconOnly
                                    Accessible.name: text
                                    Controls.ToolTip.text: text
                                    Controls.ToolTip.visible: hovered
                                    onClicked: root.togglePhoneSettings()
                                }
                            }
                        }

                        ListView {
                            id: threadList
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            clip: true
                            model: root.bridge.threads
                            currentIndex: -1

                            delegate: Controls.ItemDelegate {
                                id: threadDelegate
                                required property var modelData
                                width: threadList.width
                                highlighted: root.selectedThreadKey === modelData.key
                                Accessible.name: threadDelegate.modelData.name
                                onClicked: root.selectedThreadKey = modelData.key
                                contentItem: RowLayout {
                                    spacing: Kirigami.Units.smallSpacing

                                    Kirigami.Icon {
                                        source: threadDelegate.modelData.is_group
                                            ? "system-users" : "user-identity"
                                        implicitWidth: Kirigami.Units.iconSizes.smallMedium
                                        implicitHeight: implicitWidth
                                    }
                                    ColumnLayout {
                                        Layout.fillWidth: true
                                        spacing: 0
                                        Controls.Label {
                                            id: preview
                                            Layout.fillWidth: true
                                            text: threadDelegate.modelData.name
                                            textFormat: Text.PlainText
                                            font.bold: root.threadIsUnread(threadDelegate.modelData)
                                            elide: Text.ElideRight
                                        }
                                        Controls.Label {
                                            Layout.fillWidth: true
                                            text: threadDelegate.modelData.messages.length
                                                ? threadDelegate.modelData.messages[threadDelegate.modelData.messages.length - 1].body
                                                : qsTr("No Messages")
                                            textFormat: Text.PlainText
                                            opacity: 0.7
                                            elide: Text.ElideRight
                                        }
                                    }
                                    Controls.ToolButton {
                                        icon.name: threadDelegate.modelData.starred
                                            ? "starred-symbolic" : "non-starred-symbolic"
                                        Accessible.name: threadDelegate.modelData.starred
                                            ? qsTr("Unstar Conversation")
                                            : qsTr("Star Conversation")
                                        onClicked: root.bridge.setThreadStarred(
                                            threadDelegate.modelData.key,
                                            !threadDelegate.modelData.starred
                                        )
                                    }
                                }
                                TapHandler {
                                    acceptedButtons: Qt.RightButton
                                    onTapped: eventPoint => {
                                        threadContextMenu.threadKey = threadDelegate.modelData.key
                                        threadContextMenu.popup(
                                            threadDelegate,
                                            eventPoint.position.x,
                                            eventPoint.position.y
                                        )
                                    }
                                }
                            }

                            Kirigami.PlaceholderMessage {
                                anchors.centerIn: parent
                                width: parent.width - Kirigami.Units.largeSpacing * 2
                                visible: threadList.count === 0
                                icon.name: "mail-message-new"
                                text: root.bridge.status.storage_policy === "none"
                                    ? qsTr("Conversation History Disabled")
                                    : root.retainedStorageUnavailable()
                                        ? qsTr("Conversation History Unavailable")
                                        : qsTr("No Conversations Yet")
                                explanation: root.bridge.status.storage_policy === "none"
                                    ? qsTr("Local messages are not being retained. Choose a storage option in Settings to keep conversation history.")
                                    : root.retainedStorageUnavailable()
                                        ? root.storageDetail()
                                        : qsTr("New iPhone messages will appear here.")
                            }
                        }
                    }

                    ColumnLayout {
                        Controls.SplitView.fillWidth: true
                        Controls.SplitView.minimumWidth: messagesPage.narrow
                            ? 0 : Kirigami.Units.gridUnit * 18
                        visible: !messagesPage.narrow || root.selectedThreadKey !== ""
                        spacing: 0

                        Controls.ToolBar {
                            Layout.fillWidth: true

                            contentItem: RowLayout {
                                Controls.ToolButton {
                                    visible: messagesPage.narrow
                                    icon.name: "go-previous"
                                    text: qsTr("Back")
                                    display: Controls.AbstractButton.IconOnly
                                    Accessible.name: text
                                    Controls.ToolTip.text: text
                                    Controls.ToolTip.visible: hovered
                                    onClicked: root.selectedThreadKey = ""
                                }
                                ColumnLayout {
                                    Layout.fillWidth: true
                                    Controls.Label {
                                        Layout.fillWidth: true
                                        text: messagesPage.thread ? messagesPage.thread.name : qsTr("Conversation")
                                        textFormat: Text.PlainText
                                        font.bold: true
                                        elide: Text.ElideRight
                                    }
                                    Controls.Label {
                                        Layout.fillWidth: true
                                        visible: messagesPage.thread !== null && !messagesPage.thread.is_group
                                        text: visible ? qsTr("Reply to: %1").arg(messagesPage.thread.recipients.join(", ")) : ""
                                        textFormat: Text.PlainText
                                        elide: Text.ElideRight
                                        opacity: 0.7
                                    }
                                }
                                Controls.ToolButton {
                                    visible: messagesPage.thread !== null
                                        && messagesPage.thread.group_origin === "named"
                                    icon.name: "system-users"
                                    text: qsTr("Edit Group Participants")
                                    display: Controls.AbstractButton.IconOnly
                                    Accessible.name: text
                                    Controls.ToolTip.text: text
                                    Controls.ToolTip.visible: hovered
                                    onClicked: {
                                        groupParticipantsDialog.thread = messagesPage.thread
                                        groupParticipantsDialog.open()
                                    }
                                }
                                Controls.ToolButton {
                                    icon.name: "settings-configure"
                                    text: qsTr("Settings")
                                    display: Controls.AbstractButton.IconOnly
                                    Accessible.name: text
                                    Controls.ToolTip.text: text
                                    Controls.ToolTip.visible: hovered
                                    onClicked: root.togglePhoneSettings()
                                }
                            }
                        }

                        Controls.Label {
                            Layout.fillWidth: true
                            visible: messagesPage.thread !== null
                                && messagesPage.thread.messages_truncated === true
                            text: qsTr("Showing recent messages. Older messages remain in local history.")
                            wrapMode: Text.Wrap
                        }

                        ListView {
                            id: messageList
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            clip: true
                            spacing: Kirigami.Units.smallSpacing
                            model: messagesPage.thread ? messagesPage.thread.messages : []
                            verticalLayoutDirection: ListView.TopToBottom

                            delegate: Item {
                                id: messageDelegate
                                required property var modelData
                                width: messageList.width
                                implicitHeight: bubble.implicitHeight + Kirigami.Units.smallSpacing * 2

                                MessageBubble {
                                    id: bubble
                                    message: messageDelegate.modelData
                                    availableWidth: messageList.width
                                    showSender: messagesPage.thread !== null
                                        && messagesPage.thread.is_group
                                    anchors.right: messageDelegate.modelData.outgoing ? parent.right : undefined
                                    anchors.left: messageDelegate.modelData.outgoing ? undefined : parent.left
                                    anchors.margins: Kirigami.Units.largeSpacing
                                }
                            }

                            Kirigami.PlaceholderMessage {
                                anchors.centerIn: parent
                                width: parent.width - Kirigami.Units.largeSpacing * 4
                                visible: messagesPage.thread === null
                                text: qsTr("Select a Conversation")
                            }

                            onCountChanged: positionViewAtEnd()
                        }

                        Kirigami.Separator { Layout.fillWidth: true }

                        Kirigami.InlineMessage {
                            Layout.fillWidth: true
                            Layout.margins: Kirigami.Units.smallSpacing
                            visible: messagesPage.thread !== null
                                && messagesPage.thread.participants_required === true
                            type: Kirigami.MessageType.Information
                            text: messagesPage.thread
                                ? messagesPage.thread.roster_changed
                                    ? root.escapedRichText(
                                        qsTr("%1 is not in BlueFerry's saved participant list for %2. Review the list before replying.")
                                            .arg(root.htmlEscape(messagesPage.thread.unexpected_sender || qsTr("Someone new")))
                                            .arg(root.htmlEscape(messagesPage.thread.name))
                                      )
                                    : root.escapedRichText(
                                        qsTr("%1 has sent a message to the group %2. BlueFerry needs its participant list before you can reply.")
                                            .arg(root.htmlEscape(messagesPage.thread.prompt_sender || qsTr("Someone")))
                                            .arg(root.htmlEscape(messagesPage.thread.name))
                                      )
                                : ""
                            actions: [Kirigami.Action {
                                text: qsTr("Add Participants")
                                icon.name: "list-add-user"
                                onTriggered: {
                                    groupParticipantsDialog.thread = messagesPage.thread
                                    groupParticipantsDialog.open()
                                }
                            }]
                        }

                        RowLayout {
                            Layout.fillWidth: true
                            Layout.margins: Kirigami.Units.smallSpacing

                            ExpandingMessageComposer {
                                id: composer
                                Connections {
                                    target: root.bridge
                                    function onThreadSendSucceeded(key: string, body: string): void {
                                        if (messagesPage.thread && messagesPage.thread.key === key
                                                && composer.text === body)
                                            composer.clear()
                                    }
                                }
                                placeholderText: qsTr("Write a Message")
                                enabled: messagesPage.thread !== null
                                    && messagesPage.thread.reply_ready && !root.bridge.busy
                                Accessible.name: qsTr("Message Text")
                                onAccepted: sendButton.clicked()
                            }
                            Controls.Button {
                                id: sendButton
                                Layout.alignment: Qt.AlignBottom
                                text: qsTr("Send")
                                icon.name: "document-send"
                                enabled: composer.enabled && composer.text.trim() !== ""
                                Accessible.name: qsTr("Send Message")
                                onClicked: {
                                    root.bridge.sendThread(
                                        messagesPage.thread.key,
                                        composer.text,
                                        false
                                    )
                                }
                            }
                        }
                    }
                }
        }
    }

    Component {
        id: aboutPage

        Kirigami.AboutPage {
            aboutData: ({
                displayName: qsTr("BlueFerry"),
                productName: "BlueFerry",
                componentName: "BlueFerry",
                shortDescription: qsTr("Messages, contacts, and notifications from a paired iPhone"),
                homepage: "https://github.com/erikwb/blueferry",
                bugAddress: "https://github.com/erikwb/blueferry/issues",
                version: root.bridge.version,
                otherText: "",
                authors: [],
                credits: [],
                translators: [],
                licenses: [{name: "GPL-2.0-or-later", text: "", spdx: "GPL-2.0-or-later"}],
                copyrightStatement: qsTr("Copyright © 2026 Erik Bourget <erik@ebourget.net>\nCopyright © 2026 Gabe Shatunovsky <gabriel@shatunovsky.com>"),
                desktopFileName: "io.weirdware.BlueFerry.Qt"
            })
        }
    }

    Loader {
        id: iphonePageLoader
        // PageRow creates Component-backed pages under an internal QtObject.
        // Keep this page visually parented and alive so Qt does not warn while
        // Kirigami is still incubating its toolbar delegates.
        asynchronous: false
        visible: false
        sourceComponent: iphonePageComponent
    }

    Component {
        id: iphonePageComponent

        Kirigami.ScrollablePage {
            id: iphonePage
            title: qsTr("iPhone Settings")
        property int selectedDevice: -1
        property var device: selectedDevice >= 0 && selectedDevice < root.bridge.devices.length
            ? root.bridge.devices[selectedDevice]
            : null
        property bool hasMultipleAdapters: (root.bridge.compatibility.adapters || []).length > 1

        function syncAdapterCombo() {
            const adapters = root.bridge.compatibility.adapters || []
            const current = root.bridge.compatibility.adapter
            for (let index = 0; index < adapters.length; ++index) {
                if (adapters[index].name === current) {
                    adapterCombo.currentIndex = index
                    return
                }
            }
        }
        property var configuredDevice: {
            for (let index = 0; index < root.bridge.devices.length; ++index) {
                if (root.bridge.devices[index].mac === root.bridge.configuredMac)
                    return root.bridge.devices[index]
            }
            return null
        }
        property string effectiveStage: root.bridge.onboardingStage
        property bool compatibilityModeOverride: false

        ColumnLayout {
            width: parent.width
            spacing: Kirigami.Units.largeSpacing

                RowLayout {
                    Layout.fillWidth: true

                    Item { Layout.fillWidth: true }
                    Controls.ToolButton {
                        icon.name: "window-close"
                        text: qsTr("Close Settings")
                        Accessible.name: text
                        Controls.ToolTip.text: text
                        Controls.ToolTip.visible: hovered
                        onClicked: root.closePhoneSettings()
                    }
                }

                Kirigami.InlineMessage {
                    Layout.fillWidth: true
                    visible: root.bridge.errorText !== ""
                    text: root.htmlEscape(root.bridge.errorText)
                    type: Kirigami.MessageType.Error
                }

                Kirigami.Heading {
                    text: root.bridge.configured
                        ? qsTr("Your iPhone") : qsTr("Connect an iPhone")
                    level: 2
                }
                Controls.Label {
                    Layout.fillWidth: true
                    wrapMode: Text.Wrap
                    visible: !root.bridge.configured
                    text: qsTr("Keep the iPhone unlocked with its Bluetooth settings open during pairing.")
                }

                Connections {
                    target: root.bridge
                    function onCompatibilityChanged() { iphonePage.syncAdapterCombo() }
                    function onDevicesChanged() {
                        deviceCombo.currentIndex = root.bridge.devices.length > 0 ? 0 : -1
                        iphonePage.selectedDevice = deviceCombo.currentIndex
                    }
                }

                OnboardingSummary {
                    id: onboardingSummary
                    Layout.fillWidth: true
                    stage: compatibilityMode.checked
                        && iphonePage.effectiveStage === "activate-bluetooth"
                        ? "select-device" : iphonePage.effectiveStage
                    compatibility: root.bridge.onboardingCompatibility
                    status: root.bridge.status
                    storagePolicy: root.bridge.status.storage_policy || ""
                    storageState: root.bridge.status.storage_state || ""
                }

                Kirigami.FormLayout {
                    Layout.fillWidth: true
                    visible: !root.bridge.configured

                    Controls.Label {
                        Kirigami.FormData.label: qsTr("Controller:")
                        visible: !iphonePage.hasMultipleAdapters
                        text: root.bridge.compatibility.adapter || qsTr("Checking…")
                        textFormat: Text.PlainText
                    }

                    Controls.ComboBox {
                        id: adapterCombo
                        Kirigami.FormData.label: qsTr("Controller:")
                        visible: iphonePage.hasMultipleAdapters
                        model: root.bridge.compatibility.adapters || []
                        textRole: "label"
                        valueRole: "name"
                        enabled: !root.bridge.busy
                        onActivated: {
                            if (currentValue)
                                root.bridge.selectAdapter(currentValue)
                        }
                        Component.onCompleted: iphonePage.syncAdapterCombo()
                    }

                    Controls.Label {
                        Kirigami.FormData.label: qsTr("Hardware:")
                        text: !root.bridge.compatibilityLoaded
                            ? qsTr("Checking…")
                            : root.bridge.compatibility.available !== true
                            ? qsTr("Could Not Verify — Pairing Still Available")
                            : root.bridge.compatibility.hardware_supported
                                ? qsTr("Compatible")
                                : qsTr("Compatibility Warning — Pairing Still Available")
                    }

                    Controls.Label {
                        Kirigami.FormData.label: qsTr("Messages and Contacts:")
                        text: root.bridge.compatibility.messages_supported
                            ? qsTr("Supported") : qsTr("Not Detected")
                    }

                    Controls.Label {
                        Kirigami.FormData.label: qsTr("iPhone Notifications:")
                        text: root.bridge.compatibility.notifications_supported
                            ? qsTr("Supported") : qsTr("Unavailable")
                    }

                    Controls.Label {
                        Kirigami.FormData.label: qsTr("Bluetooth Support:")
                        text: compatibilityMode.checked
                            || !root.bridge.compatibility.notifications_supported
                            ? qsTr("Not Required")
                            : root.bridge.bluetoothActive
                                ? qsTr("Active")
                                : qsTr("Restart Required")
                    }
                }

                RowLayout {
                    visible: !root.bridge.configured
                    Controls.Button {
                        visible: root.bridge.compatibility.notifications_supported === true
                            && !root.bridge.bluetoothActive
                            && !compatibilityMode.checked
                        text: qsTr("Restart Bluetooth")
                        icon.name: "network-bluetooth"
                        enabled: !root.bridge.busy
                        onClicked: restartBluetoothDialog.open()
                    }
                    Controls.Button {
                        text: qsTr("1. Scan for iPhone")
                        icon.name: "edit-find"
                        enabled: !root.bridge.busy
                        onClicked: root.bridge.loadDevices(true)
                    }
                    Controls.BusyIndicator {
                        running: root.bridge.busy
                        visible: running
                    }
                }

                Kirigami.FormLayout {
                    Layout.fillWidth: true
                    visible: !root.bridge.configured

                    Controls.ComboBox {
                        id: deviceCombo
                        Kirigami.FormData.label: qsTr("Found iPhone:")
                        model: root.bridge.devices
                        textRole: "display_name"
                        valueRole: "mac"
                        enabled: !root.bridge.busy
                        // org.kde.desktop paints displayText in the StyleItem
                        // background. A contentItem Label would overlay a second copy.
                        delegate: Controls.ItemDelegate {
                            id: deviceOption
                            required property var modelData
                            width: deviceCombo.width
                            Accessible.name: deviceOption.modelData.display_name
                            contentItem: Controls.Label {
                                text: deviceOption.modelData.display_name
                                textFormat: Text.PlainText
                                elide: Text.ElideRight
                            }
                        }
                        onCurrentIndexChanged: iphonePage.selectedDevice = currentIndex
                    }
                }

                Controls.CheckBox {
                    id: compatibilityMode
                    Layout.fillWidth: true
                    visible: !root.bridge.configured
                    text: qsTr("Compatibility pairing for iOS 18 or earlier")
                    checked: root.bridge.compatibilityLoaded
                        && (root.bridge.compatibility.notifications_supported !== true
                            || iphonePage.compatibilityModeOverride)
                    enabled: root.bridge.compatibilityLoaded
                        && root.bridge.compatibility.notifications_supported === true
                        && !root.bridge.busy
                    onClicked: iphonePage.compatibilityModeOverride = checked
                    Accessible.description: qsTr("Sets up Messages and Contacts without connecting ANCS.")
                }

                Controls.CheckBox {
                    id: explicitPairing
                    Layout.fillWidth: true
                    visible: !root.bridge.configured
                    text: qsTr("Use explicit Bluetooth pairing")
                    enabled: !root.bridge.busy
                    Accessible.description: qsTr("Skips the initial Bluetooth connection attempt and calls Pair immediately. Try this for controllers that cancel normal pairing.")
                }

                RowLayout {
                    visible: !root.bridge.configured
                    Controls.Button {
                        text: iphonePage.device !== null && iphonePage.device.paired
                            ? qsTr("Use Existing Pairing") : qsTr("2. Pair Selected iPhone")
                        icon.name: "network-connect"
                        enabled: iphonePage.device !== null
                            && root.bridge.compatibilityLoaded
                            && !root.bridge.busy
                        onClicked: {
                            if (!iphonePage.device.paired && root.bridge.targetSaved) {
                                replaceTargetDialog.mac = iphonePage.device.mac
                                replaceTargetDialog.compatibilityMode = compatibilityMode.checked
                                replaceTargetDialog.explicitPairing = explicitPairing.checked
                                replaceTargetDialog.open()
                            } else {
                                root.bridge.completePairing(
                                    iphonePage.device.mac,
                                    compatibilityMode.checked,
                                    explicitPairing.checked
                                )
                            }
                        }
                    }
                    Controls.Button {
                        text: qsTr("Forget")
                        icon.name: "edit-delete-remove"
                        enabled: iphonePage.device !== null
                            && iphonePage.device.paired && !root.bridge.busy
                        onClicked: {
                            forgetDialog.mac = iphonePage.device.mac
                            forgetDialog.open()
                        }
                    }
                }

                Controls.Label {
                    Layout.fillWidth: true
                    visible: !root.bridge.configured && compatibilityMode.checked
                    wrapMode: Text.Wrap
                    text: qsTr("BlueFerry will still advertise ANCS solicitation so the iPhone exposes its Messages and Contacts permissions, but it will not connect system notifications.")
                }

                Controls.Button {
                    visible: root.bridge.pairingIssueReport !== ""
                    text: qsTr("Report Pairing Issue")
                    icon.name: "help-about"
                    onClicked: pairingIssueDialog.open()
                }

                RowLayout {
                    Layout.fillWidth: true
                    visible: root.bridge.configured

                    Controls.Label {
                        Layout.fillWidth: true
                        text: iphonePage.configuredDevice !== null
                            ? iphonePage.configuredDevice.name
                            : qsTr("iPhone")
                        textFormat: Text.PlainText
                    }
                    Controls.Button {
                        text: qsTr("Unpair")
                        icon.name: "network-disconnect"
                        enabled: !root.bridge.busy
                        onClicked: {
                            forgetDialog.mac = root.bridge.configuredMac
                            forgetDialog.open()
                        }
                    }
                }

                Kirigami.Heading { text: qsTr("Connection Health"); level: 2 }
                Kirigami.FormLayout {
                    Layout.fillWidth: true

                    Controls.Label {
                        Kirigami.FormData.label: qsTr("Background Service:")
                        text: root.bridge.status.daemon ? qsTr("Running") : qsTr("Unavailable")
                    }
                    Controls.Label {
                        Kirigami.FormData.label: qsTr("Messages:")
                        Layout.fillWidth: root.mapConnectionRefused()
                        wrapMode: Text.Wrap
                        text: root.mapConnectionRefused()
                            ? qsTr("iPhone is refusing message connections; is it connected to another computer?")
                            : root.bridge.status.map ? qsTr("Connected") : qsTr("Unavailable")
                    }
                    Controls.Label {
                        Kirigami.FormData.label: qsTr("Contacts:")
                        text: root.bridge.status.pbap ? qsTr("Connected") : qsTr("Unavailable")
                    }
                    Controls.Label {
                        Kirigami.FormData.label: qsTr("iPhone Notifications:")
                        text: root.bridge.status.ancs ? qsTr("Connected") : qsTr("Unavailable")
                    }
                    Controls.Label {
                        Kirigami.FormData.label: qsTr("Contact Destinations:")
                        text: root.bridge.status.contacts || "0"
                    }
                }
                Kirigami.InlineMessage {
                    Layout.fillWidth: true
                    visible: root.bridge.configured === true
                        && root.bridge.onboardingCompatibility.notifications_supported === true
                        && root.bridge.status.map === true
                        && root.bridge.status.pbap === true
                        && root.bridge.status.ancs === false
                    type: onboardingSummary.ancsLimitedController()
                        ? Kirigami.MessageType.Positive
                        : Kirigami.MessageType.Information
                    text: onboardingSummary.ancsUnavailableHint()
                }

                Kirigami.Heading { text: qsTr("Desktop Notifications"); level: 2 }
                Controls.Label {
                    Layout.fillWidth: true
                    wrapMode: Text.Wrap
                    text: qsTr("Choose which iPhone events create desktop popups. Messages only is the default.")
                }
                Kirigami.FormLayout {
                    Layout.fillWidth: true

                    Controls.ComboBox {
                        Kirigami.FormData.label: qsTr("Show Popups:")
                        textRole: "text"
                        valueRole: "value"
                        model: [
                            { "text": qsTr("All iPhone Notifications"), "value": "all" },
                            { "text": qsTr("Messages Only"), "value": "messages" },
                            { "text": qsTr("None"), "value": "none" }
                        ]
                        currentIndex: root.bridge.status.notification_policy === "all" ? 0
                            : root.bridge.status.notification_policy === "none" ? 2 : 1
                        enabled: root.bridge.status.daemon === true && !root.bridge.busy
                        onActivated: root.bridge.setNotificationPolicy(currentValue)
                    }
                    Controls.CheckBox {
                        Layout.fillWidth: true
                        text: qsTr("Only notify for contacts")
                        checked: root.bridge.status.contacts_only_notifications === true
                        enabled: root.bridge.status.daemon === true
                            && root.bridge.status.notification_policy !== "none"
                            && !root.bridge.busy
                        onClicked: root.bridge.setContactsOnlyNotifications(checked)
                        Accessible.description: qsTr("Unknown senders remain available in message history.")
                    }
                }

                Kirigami.Heading { text: qsTr("Local Data"); level: 2 }
                Kirigami.FormLayout {
                    Layout.fillWidth: true

                    Controls.ComboBox {
                        Kirigami.FormData.label: qsTr("Storage:")
                        textRole: "text"
                        valueRole: "value"
                        model: [
                            { "text": qsTr("Encrypted with Desktop Keyring"), "value": "encrypted" },
                            { "text": qsTr("Unencrypted Local Data"), "value": "plaintext" },
                            { "text": qsTr("Do Not Retain Local Data"), "value": "none" }
                        ]
                        currentIndex: root.bridge.status.storage_policy === "plaintext" ? 1
                            : root.bridge.status.storage_policy === "none" ? 2 : 0
                        enabled: root.bridge.status.daemon === true && !root.bridge.busy
                        onActivated: {
                            if (currentValue === root.bridge.status.storage_policy) return
                            storageChangeDialog.requestedPolicy = currentValue
                            storageChangeDialog.open()
                        }
                    }

                    Controls.Label {
                        Kirigami.FormData.label: qsTr("Status:")
                        Layout.fillWidth: true
                        text: root.storageStatusText()
                        textFormat: Text.PlainText
                    }

                    Controls.Label {
                        Kirigami.FormData.label: qsTr("Details:")
                        Layout.fillWidth: true
                        visible: root.bridge.status.storage_detail !== undefined
                            && root.bridge.status.storage_detail !== ""
                        text: root.bridge.status.storage_detail || ""
                        textFormat: Text.PlainText
                        wrapMode: Text.Wrap
                    }

                    Controls.Button {
                        Kirigami.FormData.label: qsTr("Keyring:")
                        visible: root.bridge.status.storage_policy === "encrypted"
                            && root.bridge.status.storage_state !== "ready"
                        text: qsTr("Unlock Local Data")
                        icon.name: "document-decrypt"
                        enabled: root.bridge.status.daemon === true && !root.bridge.busy
                        onClicked: root.bridge.unlockStorage()
                    }
                }

                Kirigami.Heading { text: qsTr("Maintenance"); level: 2 }
                RowLayout {
                    Controls.Button {
                        text: qsTr("Restart Service")
                        icon.name: "system-reboot"
                        enabled: !root.bridge.busy
                        onClicked: root.bridge.restartBackend()
                    }
                    Controls.Button {
                        text: qsTr("Sync Contacts")
                        icon.name: "view-refresh"
                        enabled: !root.bridge.busy
                        onClicked: root.bridge.syncContacts()
                    }
                    Controls.Button {
                        text: qsTr("Clear History")
                        icon.name: "edit-clear-history"
                        enabled: !root.bridge.busy
                        onClicked: clearDialog.open()
                    }
                }
            }
        }
    }
}
