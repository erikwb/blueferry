pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import "ui"

// Contacts tab: the cached iPhone contact cards with a search field, loaded
// page by page, and the open card with every number, address and its photo.
// Main asks the bridge to read cards only while this tab is shown. Rows,
// sections and wording come from contacts_view (via contacts_model); every
// field comes from the iPhone and is rendered as plain text.
Item {
    id: tab
    objectName: "contactsTab"
    required property var bridge
    signal callRequested(string number)
    signal messageRequested(string address)

    readonly property var view: tab.bridge.contacts || ({})
    readonly property var card: tab.bridge.contactCard || ({})
    readonly property bool cardOpen: tab.card.open === true
    readonly property bool narrow: width < Kirigami.Units.gridUnit * 30
    readonly property bool canCall: (tab.bridge.status || ({})).calls_enabled === true
        && tab.bridge.callsState === "ready"

    // The first number that can be dialled or messaged, for the header buttons.
    function primaryNumber(): string {
        const sections = tab.card.sections || []
        for (let index = 0; index < sections.length; ++index) {
            if (sections[index].id !== "phones")
                continue
            const rows = sections[index].rows || []
            for (let row = 0; row < rows.length; ++row) {
                if ((rows[row].actions || []).indexOf("dial") >= 0)
                    return rows[row].target
            }
        }
        return ""
    }

    function runAction(action: string, target: string): void {
        if (action === "dial")
            tab.callRequested(target)
        else if (action === "message")
            tab.messageRequested(target)
        else if (action === "mail")
            Qt.openUrlExternally("mailto:" + encodeURIComponent(target))
        else if (action === "copy") {
            copyHelper.text = target
            copyHelper.selectAll()
            copyHelper.copy()
            copyHelper.text = ""
        }
    }

    function actionText(action: string): string {
        switch (action) {
        case "dial": return qsTr("Call")
        case "message": return qsTr("Send a message")
        case "mail": return qsTr("Write an email")
        default: return qsTr("Copy")
        }
    }

    function actionIcon(action: string): string {
        switch (action) {
        case "dial": return "call-start"
        case "message": return "dialog-messages"
        case "mail": return "mail-message-new"
        default: return "edit-copy"
        }
    }

    // Clipboard access without a widget dependency.
    TextEdit {
        id: copyHelper
        visible: false
        textFormat: TextEdit.PlainText
    }

    EmptyState {
        objectName: "contactsUnsupported"
        anchors.centerIn: parent
        visible: tab.bridge.contactsSupported !== true
        dimmed: true
        icon.name: "view-pim-contacts"
        text: qsTr("Contacts Unavailable")
        explanation: qsTr("The running BlueFerry service does not offer contact cards. Restart it after upgrading.")
    }

    RowLayout {
        anchors.fill: parent
        spacing: 0
        visible: tab.bridge.contactsSupported === true

        ColumnLayout {
            objectName: "contactListPane"
            Layout.fillHeight: true
            Layout.fillWidth: tab.narrow
            Layout.preferredWidth: tab.narrow ? tab.width : Math.max(
                Kirigami.Units.gridUnit * 14, tab.width * 0.38)
            visible: !tab.narrow || !tab.cardOpen
            spacing: 0

            Kirigami.SearchField {
                id: contactSearch
                objectName: "contactSearchField"
                Layout.fillWidth: true
                Layout.margins: Kirigami.Units.largeSpacing
                placeholderText: tab.view.placeholder || ""
                maximumLength: 128
                Accessible.name: placeholderText
                // The bridge waits for a pause in typing before it reads.
                onTextChanged: tab.bridge.setContactQuery(text)
            }

            Controls.Label {
                objectName: "contactCount"
                Layout.fillWidth: true
                Layout.leftMargin: Kirigami.Units.largeSpacing
                Layout.rightMargin: Kirigami.Units.largeSpacing
                visible: text !== ""
                text: tab.view.count_text || ""
                textFormat: Text.PlainText
                font: Kirigami.Theme.smallFont
                color: Kirigami.Theme.disabledTextColor
            }

            Kirigami.InlineMessage {
                objectName: "contactsError"
                Layout.fillWidth: true
                Layout.margins: Kirigami.Units.smallSpacing
                visible: (tab.view.error || "") !== ""
                type: Kirigami.MessageType.Error
                text: tab.view.error || ""
                actions: [Kirigami.Action {
                    text: qsTr("Try Again")
                    icon.name: "view-refresh"
                    onTriggered: tab.bridge.reloadContacts()
                }]
            }

            ListView {
                id: contactList
                objectName: "contactList"
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                model: tab.view.rows || []
                currentIndex: -1
                topMargin: Kirigami.Units.smallSpacing
                bottomMargin: Kirigami.Units.smallSpacing
                leftMargin: Kirigami.Units.smallSpacing
                rightMargin: Kirigami.Units.smallSpacing
                Controls.ScrollBar.vertical: Controls.ScrollBar {}
                // Next page when the end of the list comes into view.
                onAtYEndChanged: if (atYEnd && count > 0 && tab.view.has_more === true)
                    tab.bridge.loadMoreContacts()

                delegate: ListRow {
                    id: contactRow
                    objectName: "contactDelegate"
                    required property var modelData
                    partPrefix: "contact"
                    density: "compact"
                    highlighted: tab.card.id === contactRow.modelData.id && tab.cardOpen
                    title: contactRow.modelData.title
                    subtitle: contactRow.modelData.subtitle
                    leading: Component {
                        CardAvatar {
                            initials: contactRow.modelData.initials
                        }
                    }
                    onClicked: tab.bridge.openContact(contactRow.modelData.id)
                }

                footer: Controls.ToolButton {
                    objectName: "contactLoadMore"
                    width: ListView.view ? ListView.view.width : implicitWidth
                    visible: tab.view.has_more === true
                    height: visible ? implicitHeight : 0
                    text: qsTr("Show More Contacts")
                    enabled: tab.view.loading !== true
                    onClicked: tab.bridge.loadMoreContacts()
                }

                EmptyState {
                    objectName: "contactsEmpty"
                    anchors.centerIn: parent
                    visible: contactList.count === 0 && (tab.view.hint || "") !== ""
                    icon.name: tab.view.query ? "edit-find" : "view-pim-contacts"
                    text: tab.view.query ? qsTr("No Matching Contacts") : qsTr("No Contacts Yet")
                    explanation: tab.view.hint || ""
                }

                Controls.BusyIndicator {
                    anchors.centerIn: parent
                    visible: contactList.count === 0 && tab.view.loading === true
                    running: visible
                }
            }
        }

        Kirigami.Separator {
            Layout.fillHeight: true
            visible: !tab.narrow
        }

        ColumnLayout {
            objectName: "contactCardPane"
            Layout.fillWidth: true
            Layout.fillHeight: true
            visible: !tab.narrow || tab.cardOpen
            spacing: 0

            EmptyState {
                Layout.fillWidth: true
                Layout.fillHeight: true
                visible: !tab.cardOpen
                icon.name: "view-pim-contacts"
                text: qsTr("Select a Contact")
            }

            RowLayout {
                objectName: "contactCardHeader"
                Layout.fillWidth: true
                Layout.margins: Kirigami.Units.largeSpacing
                visible: tab.cardOpen
                spacing: Kirigami.Units.largeSpacing

                Controls.ToolButton {
                    objectName: "contactBack"
                    visible: tab.narrow
                    icon.name: "go-previous"
                    text: qsTr("Back")
                    display: Controls.AbstractButton.IconOnly
                    Accessible.name: text
                    Controls.ToolTip.text: text
                    Controls.ToolTip.visible: hovered
                    onClicked: tab.bridge.closeContact()
                }
                CardAvatar {
                    objectName: "contactPhoto"
                    Layout.preferredWidth: Kirigami.Units.iconSizes.huge
                    Layout.preferredHeight: Kirigami.Units.iconSizes.huge
                    initials: tab.card.initials || ""
                    source: tab.card.photo || ""
                }
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 0
                    Kirigami.Heading {
                        objectName: "contactTitle"
                        Layout.fillWidth: true
                        level: 2
                        text: tab.card.title || ""
                        textFormat: Text.PlainText
                        elide: Text.ElideRight
                    }
                    Controls.Label {
                        objectName: "contactSubtitle"
                        Layout.fillWidth: true
                        visible: text !== ""
                        text: tab.card.subtitle || ""
                        textFormat: Text.PlainText
                        elide: Text.ElideRight
                        color: Kirigami.Theme.disabledTextColor
                    }
                }
                Controls.Button {
                    objectName: "contactCallButton"
                    visible: tab.primaryNumber() !== ""
                    text: qsTr("Call")
                    icon.name: "call-start"
                    enabled: tab.canCall && !tab.bridge.busy
                    Controls.ToolTip.text: tab.canCall ? text : qsTr("Calls are not ready.")
                    Controls.ToolTip.visible: hovered
                    onClicked: tab.callRequested(tab.primaryNumber())
                }
                Controls.Button {
                    objectName: "contactMessageButton"
                    visible: tab.primaryNumber() !== ""
                    text: qsTr("Message")
                    icon.name: "dialog-messages"
                    onClicked: tab.messageRequested(tab.primaryNumber())
                }
            }

            Kirigami.InlineMessage {
                objectName: "contactCardError"
                Layout.fillWidth: true
                Layout.margins: Kirigami.Units.smallSpacing
                visible: tab.cardOpen && (tab.card.error || "") !== ""
                type: tab.card.stale === true ? Kirigami.MessageType.Warning
                                              : Kirigami.MessageType.Error
                text: tab.card.error || ""
                actions: [Kirigami.Action {
                    text: qsTr("Reload")
                    icon.name: "view-refresh"
                    onTriggered: {
                        tab.bridge.closeContact()
                        tab.bridge.reloadContacts()
                    }
                }]
            }

            Controls.ScrollView {
                Layout.fillWidth: true
                Layout.fillHeight: true
                visible: tab.cardOpen
                Controls.ScrollBar.horizontal.policy: Controls.ScrollBar.AlwaysOff

                ColumnLayout {
                    width: parent ? parent.width : implicitWidth
                    spacing: 0

                    Repeater {
                        model: tab.card.sections || []
                        delegate: ColumnLayout {
                            id: section
                            required property var modelData
                            Layout.fillWidth: true
                            spacing: 0

                            Kirigami.ListSectionHeader {
                                Layout.fillWidth: true
                                text: section.modelData.title
                            }
                            Repeater {
                                model: section.modelData.rows
                                delegate: RowLayout {
                                    id: detail
                                    objectName: "contactDetailRow"
                                    required property var modelData
                                    Layout.fillWidth: true
                                    Layout.leftMargin: Kirigami.Units.largeSpacing
                                    Layout.rightMargin: Kirigami.Units.smallSpacing
                                    Layout.topMargin: Kirigami.Units.smallSpacing
                                    spacing: Kirigami.Units.largeSpacing

                                    ColumnLayout {
                                        Layout.fillWidth: true
                                        spacing: 0
                                        Controls.Label {
                                            Layout.fillWidth: true
                                            text: detail.modelData.label
                                            textFormat: Text.PlainText
                                            font: Kirigami.Theme.smallFont
                                            color: Kirigami.Theme.disabledTextColor
                                        }
                                        Controls.Label {
                                            objectName: "contactDetailValue"
                                            Layout.fillWidth: true
                                            text: detail.modelData.value
                                            textFormat: Text.PlainText
                                            wrapMode: Text.Wrap
                                        }
                                    }
                                    Repeater {
                                        model: detail.modelData.actions
                                        delegate: Controls.ToolButton {
                                            id: actionButton
                                            required property string modelData
                                            objectName: "contactAction_" + actionButton.modelData
                                            icon.name: tab.actionIcon(actionButton.modelData)
                                            text: tab.actionText(actionButton.modelData)
                                            display: Controls.AbstractButton.IconOnly
                                            enabled: actionButton.modelData !== "dial"
                                                || (tab.canCall && !tab.bridge.busy)
                                            Accessible.name: text
                                            Controls.ToolTip.text: text
                                            Controls.ToolTip.visible: hovered
                                            onClicked: tab.runAction(
                                                actionButton.modelData, detail.modelData.target)
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
