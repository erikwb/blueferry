pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Dialogs as Dialogs
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import "ui"

// Phone card section "From Plugins" (PLUGINS.md, capability card): the items
// every enabled card plugin offers, each with up to three action buttons.
// A plugin that crashed or answered garbage shows a dimmed hint instead.
// Everything here is plain text; calls run in the controller's plugin pool.
// An action with sendTo (ApiVersion 1.4, e.g. "Send files…" on a LocalSend
// device) opens a file dialog and sends to that target instead of invoking;
// files dropped on such an item go to the same target. "Send to…" at the
// end lists the targets of every plugin with the share capability.
ColumnLayout {
    id: section
    objectName: "pluginCardSection"
    required property var bridge

    readonly property var surfaces: section.bridge.pluginSurfaces || ({})
    readonly property var cards: section.surfaces.cards || []
    readonly property bool working: (section.surfaces.busy || "") !== ""
    // {pluginId, target, label} of the action whose file dialog is open.
    property var pendingSend: null
    // "Send to…" targets from plugins with the share capability.
    readonly property var shareTargets: section.surfaces.targets || []
    readonly property bool canShare: section.surfaces.shareAvailable === true
        || section.shareTargets.length > 0
    property string sendKey: ""

    // Asking for targets starts the share plugins: only when the menu opens.
    function loadTargets() {
        if (typeof section.bridge.loadShareTargets === "function")
            section.bridge.loadShareTargets()
    }

    // One click on an action: send files (sendTo) or let the plugin act.
    function runAction(pluginId, item, action) {
        if ((action.sendTo || "") !== "") {
            section.pendingSend = {pluginId: pluginId, target: action.sendTo, label: item.title}
            sendDialog.open()
            return
        }
        section.bridge.invokePluginAction(pluginId, item.id, action.id)
    }
    // The file dialog's answer (also called by tests).
    function finishSend(urls) {
        const pending = section.pendingSend
        section.pendingSend = null
        if (pending !== null && urls.length > 0)
            section.bridge.sendFromPluginCard(pending.pluginId, pending.target, pending.label, urls)
    }

    Layout.fillWidth: true
    spacing: 0
    visible: section.cards.length > 0 || section.canShare
        || (section.surfaces.message || "") !== ""

    Component.onCompleted: {
        if (typeof section.bridge.refreshPluginCards === "function")
            section.bridge.refreshPluginCards()
    }

    SectionHeader {
        text: qsTr("From Plugins")
        level: 4
        Controls.ToolButton {
            objectName: "pluginCardsRefresh"
            icon.name: "view-refresh"
            text: qsTr("Refresh")
            display: Controls.AbstractButton.IconOnly
            enabled: !section.working
            Accessible.name: text
            Controls.ToolTip.text: text
            Controls.ToolTip.visible: hovered
            onClicked: section.bridge.refreshPluginCards()
        }
    }

    Repeater {
        model: section.cards
        delegate: ColumnLayout {
            id: plugin
            required property var modelData
            objectName: "pluginCard_" + plugin.modelData.pluginId
            Layout.fillWidth: true
            spacing: 0

            // Plugin name only when there are several, like a sub-heading.
            Controls.Label {
                Layout.fillWidth: true
                Layout.leftMargin: Kirigami.Units.largeSpacing
                Layout.rightMargin: Kirigami.Units.largeSpacing
                visible: section.cards.length > 1 && plugin.modelData.ok
                text: plugin.modelData.name
                textFormat: Text.PlainText
                elide: Text.ElideRight
                font: Kirigami.Theme.smallFont
                color: Kirigami.Theme.disabledTextColor
            }
            ListRow {
                objectName: "pluginCardHint"
                Layout.fillWidth: true
                visible: !plugin.modelData.ok
                density: "compact"
                wrapSubtitle: true
                dimmed: true
                avatarSize: Kirigami.Units.iconSizes.smallMedium
                iconName: "dialog-warning"
                title: plugin.modelData.name
                subtitle: plugin.modelData.hint
                focusPolicy: Qt.NoFocus
                hoverEnabled: false
            }
            Repeater {
                model: plugin.modelData.items
                delegate: ListRow {
                    id: item
                    required property var modelData
                    readonly property var primary: (item.modelData.actions || [])
                        .find(action => action.primary === true) || null
                    objectName: "pluginItem_" + item.modelData.id
                    Layout.fillWidth: true
                    density: "compact"
                    wrapSubtitle: true
                    avatarSize: Kirigami.Units.iconSizes.smallMedium
                    iconName: item.modelData.icon
                    title: item.modelData.title
                    subtitle: item.modelData.subtitle
                    pinActions: true
                    enabled: !section.working
                    readonly property bool dropsFiles: (item.modelData.dropTarget || "") !== ""
                    highlighted: dropArea.containsDrag
                    // A click on the row runs the item's primary action.
                    onClicked: if (item.primary !== null)
                        section.runAction(plugin.modelData.pluginId, item.modelData, item.primary)

                    // Files dragged from the file manager onto a device.
                    DropArea {
                        id: dropArea
                        objectName: "pluginDropArea"
                        parent: item
                        anchors.fill: parent
                        enabled: item.dropsFiles && !section.working
                        // Only local files: a link dragged from a browser is no file.
                        onEntered: drag => {
                            if (!drag.hasUrls || drag.urls.some(url => !String(url).startsWith("file:")))
                                drag.accepted = false
                        }
                        onDropped: drop => {
                            if (!drop.hasUrls)
                                return
                            drop.acceptProposedAction()
                            section.bridge.sendFromPluginCard(plugin.modelData.pluginId,
                                item.modelData.dropTarget, item.modelData.title, drop.urls)
                        }
                    }

                    Repeater {
                        model: (item.modelData.actions || []).filter(action => action.primary !== true)
                        delegate: Controls.ToolButton {
                            id: actionButton
                            required property var modelData
                            objectName: "pluginAction_" + actionButton.modelData.id
                            text: actionButton.modelData.label
                            icon.name: actionButton.modelData.icon
                            display: actionButton.modelData.icon !== ""
                                ? Controls.AbstractButton.IconOnly : Controls.AbstractButton.TextOnly
                            Accessible.name: text
                            Controls.ToolTip.text: text
                            Controls.ToolTip.visible: hovered && display === Controls.AbstractButton.IconOnly
                            onClicked: section.runAction(
                                plugin.modelData.pluginId, item.modelData, actionButton.modelData)
                        }
                    }
                    Controls.ToolButton {
                        objectName: "pluginPrimaryAction"
                        visible: item.primary !== null
                        text: item.primary !== null ? item.primary.label : ""
                        icon.name: item.primary !== null && item.primary.icon !== ""
                            ? item.primary.icon : "go-next"
                        display: Controls.AbstractButton.IconOnly
                        Accessible.name: text
                        Controls.ToolTip.text: text
                        Controls.ToolTip.visible: hovered
                        onClicked: section.runAction(
                            plugin.modelData.pluginId, item.modelData, item.primary)
                    }
                }
            }
        }
    }

    Dialogs.FileDialog {
        id: sendDialog
        objectName: "pluginSendDialog"
        title: section.pendingSend !== null
            ? qsTr("Send Files to %1").arg(section.pendingSend.label) : qsTr("Choose Files to Send")
        fileMode: Dialogs.FileDialog.OpenFiles
        onAccepted: section.finishSend(selectedFiles)
        onRejected: section.pendingSend = null
    }

    ListRow {
        id: sendRow
        objectName: "sendToRow"
        Layout.fillWidth: true
        visible: section.canShare
        density: "compact"
        wrapSubtitle: true
        avatarSize: Kirigami.Units.iconSizes.smallMedium
        iconName: "document-send"
        title: qsTr("Send to…")
        subtitle: section.shareTargets.length > 0
            ? section.shareTargets.map(target => target.label).join(", ")
            : qsTr("Send files to a plugin target")
        enabled: (section.surfaces.busy || "") === ""
        onClicked: sendMenu.popup(sendRow, 0, sendRow.height)

        Controls.Menu {
            id: sendMenu
            objectName: "sendToMenu"
            onAboutToShow: section.loadTargets()
            Controls.MenuItem {
                text: qsTr("Looking for targets…")
                enabled: false
                visible: section.surfaces.targetsLoading === true && section.shareTargets.length === 0
                height: visible ? implicitHeight : 0
            }
            Controls.MenuItem {
                text: qsTr("No targets right now")
                enabled: false
                visible: section.surfaces.targetsLoading !== true
                    && section.surfaces.targetsLoaded === true && section.shareTargets.length === 0
                height: visible ? implicitHeight : 0
            }
            Repeater {
                model: section.shareTargets
                delegate: Controls.MenuItem {
                    required property var modelData
                    text: modelData.menuText || ""
                    icon.name: modelData.icon
                    onTriggered: {
                        section.sendKey = modelData.key
                        shareDialog.open()
                    }
                }
            }
        }
    }

    Dialogs.FileDialog {
        id: shareDialog
        objectName: "shareSendDialog"
        title: qsTr("Choose Files to Send")
        fileMode: Dialogs.FileDialog.OpenFiles
        onAccepted: section.bridge.sendToTarget(section.sendKey, selectedFiles)
    }

    Notice {
        id: notice
        objectName: "pluginCardMessage"
        Layout.leftMargin: Kirigami.Units.largeSpacing
        Layout.rightMargin: Kirigami.Units.largeSpacing
        Layout.topMargin: Kirigami.Units.smallSpacing
        plainText: section.surfaces.message || ""
        type: section.surfaces.messageOk === false
            ? Kirigami.MessageType.Warning : Kirigami.MessageType.Positive
        showCloseButton: true
        onVisibleChanged: {
            if (visible)
                return
            if ((section.surfaces.message || "") !== "")
                section.bridge.clearPluginCardMessage()
            visible = Qt.binding(function() { return notice.plainText !== "" })
        }
    }
}
