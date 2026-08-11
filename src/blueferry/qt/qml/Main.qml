pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

Kirigami.ApplicationWindow {
    id: root

    required property var bridge
    property string selectedThreadKey: ""
    property var pendingThread: null
    property string pendingBody: ""
    property bool firstRunRedirected: false
    property var iphoneSettingsPage: null
    property string pendingMessageHandle: ""

    visible: true
    width: 980
    height: 680
    minimumWidth: 420
    minimumHeight: 480
    title: qsTr("BlueFerry")

    function selectedThread() {
        for (let index = 0; index < bridge.threads.length; ++index) {
            if (bridge.threads[index].key === selectedThreadKey) {
                return bridge.threads[index]
            }
        }
        return null
    }

    function selectMessage(handle) {
        for (let threadIndex = 0; threadIndex < bridge.threads.length; ++threadIndex) {
            const thread = bridge.threads[threadIndex]
            for (let messageIndex = 0; messageIndex < thread.messages.length; ++messageIndex) {
                if (thread.messages[messageIndex].handle === handle) {
                    closePhoneSettings()
                    pageStack.currentIndex = 0
                    selectedThreadKey = thread.key
                    pendingMessageHandle = ""
                    return true
                }
            }
        }
        return false
    }

    function htmlEscape(value) {
        return String(value || "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
    }

    function mapConnectionRefused() {
        const status = bridge.status || ({})
        if (status.connectivity_state === "map-connection-refused")
            return true
        const detail = String(status.connectivity_detail || "").toLowerCase()
        return detail.indexOf("createsession(map)") >= 0
            && detail.indexOf("connection refused") >= 0
            && detail.indexOf("111") >= 0
    }

    function pendingIphoneSetupTasks() {
        const verified = bridge.status.verified_iphone_setup || []
        const tasks = []
        if (verified.indexOf("message-notifications") < 0)
            tasks.push(qsTr("Enable Show Message Notifications"))
        if (verified.indexOf("contacts") < 0)
            tasks.push(qsTr("Enable Sync Contacts"))
        if (bridge.compatibility.notifications_supported
                && verified.indexOf("notification-access") < 0)
            tasks.push(qsTr("Allow Notification Access when prompted"))
        return tasks
    }

    function pendingIphoneSetupText() {
        const tasks = pendingIphoneSetupTasks()
        let detail = qsTr("Open Settings → Bluetooth on the iPhone, tap ⓘ next to this computer, then finish the settings below. After approving “Allow System Notifications,” you may need to return to the Bluetooth device list and reopen this computer before the other settings appear:\n• ")
            + tasks.join("\n• ")
        const verified = bridge.status.verified_iphone_setup || []
        if (bridge.compatibility.notifications_supported
                && verified.indexOf("notification-access") < 0)
            detail += qsTr("\n\nWithout System Notification access, group texts appear as individual conversations with their sender.")
        return detail
    }

    function openPhoneSettings() {
        // Utility pages belong in Kirigami's PageRow. Its modal layers are an
        // anchored StackView internally: animated pushes warn about those
        // anchors, while forcing an Immediate push can create an empty layer.
        if (iphoneSettingsPage !== null) {
            pageStack.currentIndex = pageStack.depth - 1
            return
        }
        iphoneSettingsPage = pageStack.push(iphonePageComponent)
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

    function onboardingTitle(stage) {
        const titles = {
            "checking": qsTr("Checking Bluetooth Support"),
            "incompatible": qsTr("Bluetooth Controller Is Not Compatible"),
            "activate-bluetooth": qsTr("Activate Bluetooth Support"),
            "select-device": qsTr("Pair an iPhone"),
            "starting": qsTr("Starting the Background Service"),
            "iphone-settings": qsTr("Finish Setup on the iPhone"),
            "ready": qsTr("BlueFerry Is Connected"),
            "ready-without-ancs": qsTr("Messages Are Connected")
        }
        return titles[stage] || qsTr("Set Up BlueFerry")
    }

    function onboardingDetail(stage) {
        const details = {
            "checking": qsTr("Inspecting the selected Bluetooth controller without changing it."),
            "incompatible": bridge.compatibility.issue || qsTr("A controller with BR/EDR and secure pairing is required."),
            "activate-bluetooth": qsTr("The packaged BlueZ bearer support needs one authorized Bluetooth restart."),
            "select-device": qsTr("Scan for and select your iPhone here, then choose Pair. On the iPhone, open Settings → Bluetooth, find this computer under \"Other Devices\", tap it, and approve the matching codes. Pairing may appear idle for up to 15 seconds. System Notification access is also how BlueFerry recognizes group text threads; without it, a group text appears as a one-to-one conversation with its sender."),
            "starting": qsTr("The configured backend is starting. This normally takes a few seconds."),
            "iphone-settings": pendingIphoneSetupText(),
            "ready": qsTr("Bluetooth services and iPhone permissions have been verified."),
            "ready-without-ancs": qsTr("Messages and contacts have been verified. System notifications are unavailable, so group texts may appear as individual conversations.")
        }
        return details[stage] || ""
    }

    Connections {
        target: root.bridge

        function onPairingConfirmationRequested(passkey) {
            pairingConfirmationDialog.passkey = passkey
            pairingConfirmationDialog.open()
        }

        function onThreadsChanged() {
            if (root.selectedThreadKey !== "" && root.selectedThread() === null) {
                root.selectedThreadKey = ""
            }
            if (root.pendingMessageHandle !== "")
                root.selectMessage(root.pendingMessageHandle)
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
                    root.pageStack.push(aboutPage)
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
        id: groupDialog
        title: qsTr("Send Group Message?")
        subtitle: root.pendingThread
            ? qsTr("The iPhone will reply to these participants:\n\n")
              + root.pendingThread.recipients.map(root.htmlEscape).join("\n")
            : ""
        standardButtons: Kirigami.Dialog.Cancel
        customFooterActions: [Kirigami.Action {
            text: qsTr("Send to Group")
            icon.name: "document-send"
            onTriggered: {
                root.bridge.sendThread(root.pendingThread.key, root.pendingBody, true)
                groupDialog.close()
            }
        }]
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
        id: shortcutsDialog
        title: qsTr("Keyboard Shortcuts")
        subtitle: qsTr("Refresh — Ctrl+R\nQuit — Ctrl+Q\nKeyboard Shortcuts — Ctrl+?")
        standardButtons: Kirigami.Dialog.Close
    }

    Kirigami.Dialog {
        id: newMessageDialog
        title: qsTr("New Message")
        preferredWidth: Kirigami.Units.gridUnit * 24
        standardButtons: Kirigami.Dialog.Cancel
        customFooterActions: [Kirigami.Action {
            text: qsTr("Send")
            icon.name: "document-send"
            enabled: newRecipient.text.trim() !== ""
                && newMessageBody.text.trim() !== "" && !root.bridge.busy
            onTriggered: {
                root.bridge.sendMessage(newRecipient.text, newMessageBody.text)
                newMessageDialog.close()
            }
        }]

        onOpened: {
            newRecipient.clear()
            newMessageBody.clear()
            root.bridge.findContacts("")
            newRecipient.forceActiveFocus()
        }

        Timer {
            id: contactSearchTimer
            interval: 180
            onTriggered: root.bridge.findContacts(newRecipient.text)
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
                model: root.bridge.contactResults

                delegate: Controls.ItemDelegate {
                    id: contactDelegate
                    required property var modelData
                    width: contactResults.width
                    text: modelData.name + "\n" + (
                        modelData.address.indexOf("@") >= 0
                            ? modelData.address : "+" + modelData.address
                    )
                    onClicked: {
                        newRecipient.text = modelData.address
                        root.bridge.findContacts("")
                        newMessageBody.forceActiveFocus()
                    }
                }
            }
            Controls.Label {
                text: qsTr("Message")
                font.bold: true
            }
            Controls.TextArea {
                id: newMessageBody
                Layout.fillWidth: true
                Layout.preferredHeight: Kirigami.Units.gridUnit * 5
                placeholderText: qsTr("Write a Message")
                wrapMode: TextEdit.Wrap
                Accessible.name: qsTr("Message Text")
            }
        }
    }

    Kirigami.Page {
        id: messagesPage
        visible: false
        title: qsTr("Messages")
        padding: 0
        property bool narrow: width < 680
        property var thread: root.selectedThread()

        actions: [
            Kirigami.Action {
                text: qsTr("Settings")
                icon.name: "settings-configure"
                onTriggered: root.togglePhoneSettings()
            }
        ]

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

                RowLayout {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    spacing: 0

                    ColumnLayout {
                        Layout.fillHeight: true
                        Layout.preferredWidth: messagesPage.narrow
                            ? parent.width
                            : Math.max(Kirigami.Units.gridUnit * 14, parent.width * 0.3)
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
                                Accessible.name: preview.text
                                contentItem: RowLayout {
                                    spacing: Kirigami.Units.smallSpacing

                                    Kirigami.Icon {
                                        source: threadDelegate.modelData.is_group
                                            ? "system-users" : "user-identity"
                                        implicitWidth: Kirigami.Units.iconSizes.smallMedium
                                        implicitHeight: implicitWidth
                                    }
                                    Controls.Label {
                                        id: preview
                                        Layout.fillWidth: true
                                        text: threadDelegate.modelData.name + "\n" + (
                                            threadDelegate.modelData.messages.length
                                                ? threadDelegate.modelData.messages[threadDelegate.modelData.messages.length - 1].body
                                                : qsTr("No Messages")
                                        )
                                        textFormat: Text.PlainText
                                        maximumLineCount: 2
                                        wrapMode: Text.Wrap
                                        elide: Text.ElideRight
                                    }
                                }
                                onClicked: root.selectedThreadKey = modelData.key
                            }

                            Kirigami.PlaceholderMessage {
                                anchors.centerIn: parent
                                width: parent.width - Kirigami.Units.largeSpacing * 2
                                visible: threadList.count === 0
                                icon.name: "mail-message-new"
                                text: qsTr("No Conversations Yet")
                                explanation: qsTr("New iPhone messages will appear here.")
                            }
                        }
                    }

                    Kirigami.Separator {
                        Layout.fillHeight: true
                        visible: !messagesPage.narrow
                    }

                    ColumnLayout {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
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
                                Controls.Label {
                                    Layout.fillWidth: true
                                    text: messagesPage.thread ? messagesPage.thread.name : qsTr("Conversation")
                                    textFormat: Text.PlainText
                                    font.bold: true
                                    elide: Text.ElideRight
                                }
                            }
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

                        RowLayout {
                            Layout.fillWidth: true
                            Layout.margins: Kirigami.Units.smallSpacing

                            Controls.TextField {
                                id: composer
                                Layout.fillWidth: true
                                placeholderText: qsTr("Write a Message")
                                enabled: messagesPage.thread !== null
                                    && messagesPage.thread.reply_ready && !root.bridge.busy
                                Accessible.name: qsTr("Message Text")
                                onAccepted: sendButton.clicked()
                            }
                            Controls.Button {
                                id: sendButton
                                text: qsTr("Send")
                                icon.name: "document-send"
                                enabled: composer.enabled && composer.text.trim() !== ""
                                Accessible.name: qsTr("Send Message")
                                onClicked: {
                                    if (messagesPage.thread.is_group) {
                                        root.pendingThread = messagesPage.thread
                                        root.pendingBody = composer.text.trim()
                                        composer.clear()
                                        groupDialog.open()
                                    } else {
                                        root.bridge.sendThread(messagesPage.thread.key, composer.text, false)
                                        composer.clear()
                                    }
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
                licenses: [{name: "GPL-2.0-only", text: "", spdx: "GPL-2.0-only"}],
                copyrightStatement: qsTr("Copyright © 2026 Erik Bourget <erik@ebourget.net>\nCopyright © 2026 Gabe Shatunovsky <gabriel@shatunovsky.com>"),
                desktopFileName: "io.weirdware.BlueFerry.Qt"
            })
        }
    }

    Component {
        id: iphonePageComponent

        Kirigami.ScrollablePage {
            id: iphonePage
            title: qsTr("iPhone Settings")
        actions: [
            Kirigami.Action {
                text: qsTr("Close Settings")
                icon.name: "window-close"
                onTriggered: root.closePhoneSettings()
            }
        ]
        property int selectedDevice: -1
        property var device: selectedDevice >= 0 && selectedDevice < root.bridge.devices.length
            ? root.bridge.devices[selectedDevice]
            : null
        property var configuredDevice: {
            for (let index = 0; index < root.bridge.devices.length; ++index) {
                if (root.bridge.devices[index].mac === root.bridge.configuredMac)
                    return root.bridge.devices[index]
            }
            return null
        }
        property string effectiveStage: root.bridge.onboardingStage

        ColumnLayout {
            width: parent.width
            spacing: Kirigami.Units.largeSpacing

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

                Kirigami.InlineMessage {
                    Layout.fillWidth: true
                    visible: true
                    text: root.htmlEscape(root.onboardingTitle(iphonePage.effectiveStage))
                        + "\n" + root.htmlEscape(root.onboardingDetail(iphonePage.effectiveStage))
                    type: iphonePage.effectiveStage === "incompatible"
                        ? Kirigami.MessageType.Warning
                        : iphonePage.effectiveStage === "ready"
                            || iphonePage.effectiveStage === "ready-without-ancs"
                            ? Kirigami.MessageType.Positive
                            : Kirigami.MessageType.Information
                }

                Kirigami.Heading {
                    visible: !root.bridge.configured
                    text: qsTr("Pair an iPhone")
                    level: 2
                }
                Controls.Label {
                    Layout.fillWidth: true
                    visible: !root.bridge.configured
                    wrapMode: Text.Wrap
                    text: qsTr("Scan for and select your iPhone here, then choose Pair. On the iPhone, open Settings → Bluetooth, find this computer under \"Other Devices\", tap it, and approve the matching codes. Pairing may appear idle for up to 15 seconds.")
                }

                Kirigami.FormLayout {
                    Layout.fillWidth: true
                    visible: !root.bridge.configured

                    Controls.Label {
                        Kirigami.FormData.label: qsTr("Controller:")
                        text: root.bridge.compatibility.adapter || qsTr("Checking…")
                    }

                    Controls.Label {
                        Kirigami.FormData.label: qsTr("Hardware:")
                        text: root.bridge.compatibility.hardware_supported
                            ? qsTr("Compatible")
                            : qsTr("Unsupported")
                    }

                    Controls.Label {
                        Kirigami.FormData.label: qsTr("Messages and Contacts:")
                        text: root.bridge.compatibility.messages_supported
                            ? qsTr("Supported") : qsTr("Unsupported")
                    }

                    Controls.Label {
                        Kirigami.FormData.label: qsTr("iPhone Notifications:")
                        text: root.bridge.compatibility.notifications_supported
                            ? qsTr("Supported") : qsTr("Unsupported")
                    }

                    Controls.Label {
                        Kirigami.FormData.label: qsTr("Bluetooth Support:")
                        text: !root.bridge.compatibility.notifications_supported
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
                        onCurrentIndexChanged: iphonePage.selectedDevice = currentIndex
                    }
                }

                RowLayout {
                    visible: !root.bridge.configured
                    Controls.Button {
                        text: iphonePage.device !== null && iphonePage.device.paired
                            ? qsTr("Use Existing Pairing") : qsTr("2. Pair Selected iPhone")
                        icon.name: "network-connect"
                        enabled: iphonePage.device !== null
                            && root.bridge.compatibility.pairing_ready
                            && !root.bridge.busy
                        onClicked: root.bridge.completePairing(iphonePage.device.mac)
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

                RowLayout {
                    Layout.fillWidth: true
                    visible: root.bridge.configured

                    Controls.Label {
                        Layout.fillWidth: true
                        text: iphonePage.configuredDevice !== null
                            ? iphonePage.configuredDevice.name
                            : qsTr("iPhone")
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
                        Layout.fillWidth: true
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
