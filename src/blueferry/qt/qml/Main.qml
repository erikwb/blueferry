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

    PhoneSettingsDialogs {
        id: phoneSettingsDialogs
        anchors.fill: parent
        bridge: root.bridge
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

        PhoneSettingsPage {
            bridge: root.bridge
            onCloseRequested: root.closePhoneSettings()
            onClearHistoryRequested: clearDialog.open()
            onBluetoothRestartRequested: phoneSettingsDialogs.requestBluetoothRestart()
            onPairingIssueRequested: phoneSettingsDialogs.showPairingIssue()
            onForgetRequested: mac => phoneSettingsDialogs.requestForget(mac)
            onStoragePolicyRequested: policy => phoneSettingsDialogs.requestStoragePolicy(policy)
            onPairingRequested: (mac, paired, compatibilityMode, explicitPairing) =>
                phoneSettingsDialogs.requestPairing(mac, paired, compatibilityMode, explicitPairing)
        }
    }
}
