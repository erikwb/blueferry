pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import "ui"

// Settings > Plugins: installed plugins (status, settings form, enable,
// update, remove), the plugin store from the configured indexes, and
// installing from any https Git URL. Everything a plugin or an index says is
// shown as plain text; installs are always confirmed in installDialog first.
ColumnLayout {
    id: section
    objectName: "settingsPlugins"
    required property var bridge
    spacing: Kirigami.Units.largeSpacing

    readonly property var pluginState: section.bridge.pluginSettings || ({})
    readonly property var plugins: section.pluginState.plugins || []
    readonly property var store: section.pluginState.store || ({})
    readonly property var pending: section.pluginState.pending || ({})
    readonly property var config: section.pluginState.config || ({})
    readonly property bool working: (section.pluginState.busy || "") !== ""
    property string removeId: ""

    function isHttps(url) {
        return /^https:\/\/[^\s]+$/.test(String(url || ""))
    }

    Component.onCompleted: {
        section.bridge.loadPlugins()
        section.bridge.loadPluginStore(false)
    }

    onPendingChanged: {
        if (section.pending.id)
            installDialog.open()
        else
            installDialog.close()
    }

    // git's and pip's last line and a plugin's errors end up here: plain text.
    Notice {
        id: pluginNotice
        objectName: "pluginMessage"
        type: section.pluginState.messageOk === false
            ? Kirigami.MessageType.Error : Kirigami.MessageType.Positive
        plainText: section.pluginState.message || ""
        showCloseButton: true
        // The close button sets visible = false: clear the message and
        // restore the binding so the next message shows again.
        onVisibleChanged: {
            if (visible)
                return
            if (pluginNotice.plainText !== "")
                section.bridge.clearPluginMessage()
            visible = Qt.binding(function() { return pluginNotice.plainText !== "" })
        }
    }

    RowLayout {
        Layout.fillWidth: true
        Kirigami.Heading { text: qsTr("Installed"); level: 2; Layout.fillWidth: true }
        Controls.BusyIndicator {
            running: section.working
            visible: running
            Layout.preferredHeight: Kirigami.Units.iconSizes.medium
        }
        Controls.ToolButton {
            icon.name: "view-refresh"
            text: qsTr("Refresh")
            display: Controls.AbstractButton.IconOnly
            enabled: !section.working
            onClicked: section.bridge.loadPlugins()
            Controls.ToolTip.text: text
            Controls.ToolTip.visible: hovered
        }
    }
    Controls.Label {
        Layout.fillWidth: true
        visible: section.pluginState.loaded === true && section.plugins.length === 0
        wrapMode: Text.Wrap
        text: qsTr("No plugins installed. Add one from the list below.")
    }

    Repeater {
        model: section.plugins
        delegate: Kirigami.AbstractCard {
            id: pluginCard
            required property var modelData
            readonly property bool editing: section.config.id === pluginCard.modelData.id
            objectName: "plugin_" + pluginCard.modelData.id
            Layout.fillWidth: true

            contentItem: ColumnLayout {
                spacing: Kirigami.Units.smallSpacing
                RowLayout {
                    Layout.fillWidth: true
                    Kirigami.Heading {
                        objectName: "pluginName"
                        Layout.fillWidth: true
                        level: 3
                        text: pluginCard.modelData.name + "  " + pluginCard.modelData.version
                        textFormat: Text.PlainText
                        elide: Text.ElideRight
                    }
                    Controls.Switch {
                        objectName: "pluginEnabledSwitch"
                        text: qsTr("Enabled")
                        checked: pluginCard.modelData.enabled === true
                        enabled: !section.working
                        onToggled: section.bridge.setPluginEnabled(pluginCard.modelData.id, checked)
                    }
                }
                Controls.Label {
                    Layout.fillWidth: true
                    wrapMode: Text.Wrap
                    textFormat: Text.PlainText
                    text: [pluginCard.modelData.stateText,
                           (pluginCard.modelData.capabilities || []).join(", "),
                           pluginCard.modelData.ref ? qsTr("version %1").arg(pluginCard.modelData.ref) : ""]
                        .filter(part => part).join(" · ")
                }
                Controls.Label {
                    Layout.fillWidth: true
                    visible: (pluginCard.modelData.detail || "") !== ""
                    wrapMode: Text.Wrap
                    textFormat: Text.PlainText
                    color: Kirigami.Theme.disabledTextColor
                    text: pluginCard.modelData.detail || ""
                }
                Kirigami.UrlButton {
                    objectName: "pluginSourceLink"
                    visible: section.isHttps(pluginCard.modelData.source)
                    url: section.isHttps(pluginCard.modelData.source) ? pluginCard.modelData.source : ""
                    text: pluginCard.modelData.sourceText || ""
                }
                Controls.Label {
                    visible: !pluginCard.modelData.managed
                    wrapMode: Text.Wrap
                    Layout.fillWidth: true
                    color: Kirigami.Theme.disabledTextColor
                    text: qsTr("Installed outside BlueFerry; install it from its URL to manage updates here.")
                }
                Flow {
                    Layout.fillWidth: true
                    spacing: Kirigami.Units.smallSpacing
                    Controls.Button {
                        visible: pluginCard.modelData.hasConfig === true
                        text: pluginCard.editing ? qsTr("Close Settings") : qsTr("Settings")
                        icon.name: "configure"
                        enabled: !section.working && pluginCard.modelData.enabled === true
                        onClicked: {
                            if (pluginCard.editing) {
                                section.bridge.closePluginConfig()
                            } else {
                                section.bridge.loadPluginConfig(pluginCard.modelData.id)
                            }
                        }
                    }
                    Controls.Button {
                        objectName: "pluginLogButton"
                        text: qsTr("Show Log")
                        icon.name: "text-x-log"
                        enabled: !section.working
                        Controls.ToolTip.text: qsTr("Open what the plugin wrote to its log file")
                        Controls.ToolTip.visible: hovered
                        Controls.ToolTip.delay: Kirigami.Units.toolTipDelay
                        onClicked: section.bridge.openPluginLog(pluginCard.modelData.id)
                    }
                    Controls.Button {
                        visible: pluginCard.modelData.managed === true
                        text: qsTr("Check for Update")
                        icon.name: "update-none"
                        enabled: !section.working
                        onClicked: section.bridge.preparePluginUpdate(pluginCard.modelData.id)
                    }
                    Controls.Button {
                        visible: pluginCard.modelData.managed === true
                        text: qsTr("Remove")
                        icon.name: "edit-delete-remove"
                        enabled: !section.working
                        onClicked: {
                            section.removeId = pluginCard.modelData.id
                            removeDialog.open()
                        }
                    }
                }
                Loader {
                    Layout.fillWidth: true
                    active: pluginCard.editing
                    visible: active
                    sourceComponent: PluginConfigForm {
                        bridge: section.bridge
                        config: section.config
                        busy: section.working
                    }
                }
            }
        }
    }

    Repeater {
        model: section.pluginState.ignored || []
        delegate: Controls.Label {
            required property var modelData
            Layout.fillWidth: true
            wrapMode: Text.Wrap
            textFormat: Text.PlainText
            color: Kirigami.Theme.disabledTextColor
            text: qsTr("Ignored %1: %2").arg(modelData.name).arg(modelData.reason)
        }
    }

    // ---- store -------------------------------------------------------------

    RowLayout {
        Layout.fillWidth: true
        Kirigami.Heading { text: qsTr("Add Plugins"); level: 2; Layout.fillWidth: true }
        Controls.ToolButton {
            icon.name: "view-refresh"
            text: qsTr("Reload the plugin list")
            display: Controls.AbstractButton.IconOnly
            enabled: !section.working
            onClicked: section.bridge.loadPluginStore(true)
            Controls.ToolTip.text: text
            Controls.ToolTip.visible: hovered
        }
    }
    Repeater {
        model: section.store.problems || []
        delegate: Notice {
            required property var modelData
            objectName: "pluginStoreProblem"
            type: Kirigami.MessageType.Warning
            plainText: modelData.problem + " (" + modelData.index + ")"
        }
    }
    GridLayout {
        objectName: "pluginStore"
        Layout.fillWidth: true
        columns: Math.max(1, Math.floor(section.width / (Kirigami.Units.gridUnit * 16)))
        columnSpacing: Kirigami.Units.largeSpacing
        rowSpacing: Kirigami.Units.largeSpacing

        Repeater {
            model: section.store.items || []
            delegate: PluginStoreCard {
                Layout.fillWidth: true
                Layout.alignment: Qt.AlignTop
                busy: section.working
                onInstallRequested: id => section.bridge.installStorePlugin(id)
            }
        }
    }
    Controls.Label {
        Layout.fillWidth: true
        visible: section.store.loaded === true && (section.store.items || []).length === 0
        wrapMode: Text.Wrap
        text: qsTr("The plugin list is empty or could not be loaded.")
    }

    Kirigami.FormLayout {
        Layout.fillWidth: true
        Controls.TextField {
            id: urlField
            objectName: "pluginUrlField"
            Kirigami.FormData.label: qsTr("Own URL…")
            placeholderText: "https://github.com/…/blueferry-plugin-…"
            inputMethodHints: Qt.ImhUrlCharactersOnly | Qt.ImhNoAutoUppercase
        }
        Controls.TextField {
            id: refField
            Kirigami.FormData.label: qsTr("Tag or commit:")
            placeholderText: qsTr("newest version")
        }
        Controls.Button {
            objectName: "pluginUrlInstall"
            text: qsTr("Check and Install…")
            icon.name: "list-add"
            enabled: !section.working && section.isHttps(urlField.text.trim())
            onClicked: section.bridge.preparePluginInstall(urlField.text.trim(), refField.text.trim())
        }
    }

    Kirigami.Heading { text: qsTr("Plugin Lists"); level: 3 }
    Repeater {
        model: section.pluginState.indexes || []
        delegate: RowLayout {
            id: indexRow
            required property string modelData
            Layout.fillWidth: true
            Controls.Label {
                Layout.fillWidth: true
                text: indexRow.modelData
                textFormat: Text.PlainText
                elide: Text.ElideMiddle
            }
            Controls.ToolButton {
                icon.name: "list-remove"
                text: qsTr("Remove list")
                display: Controls.AbstractButton.IconOnly
                enabled: !section.working
                onClicked: section.bridge.setPluginIndexes(
                    (section.pluginState.indexes || []).filter(url => url !== indexRow.modelData))
                Controls.ToolTip.text: text
                Controls.ToolTip.visible: hovered
            }
        }
    }
    RowLayout {
        Layout.fillWidth: true
        Controls.TextField {
            id: indexField
            Layout.fillWidth: true
            placeholderText: qsTr("https://… plugins-index.json")
        }
        Controls.Button {
            text: qsTr("Add List")
            enabled: !section.working && section.isHttps(indexField.text.trim())
            onClicked: {
                section.bridge.setPluginIndexes((section.pluginState.indexes || []).concat([indexField.text.trim()]))
                indexField.text = ""
            }
        }
        Controls.Button {
            text: qsTr("Default")
            enabled: !section.working
            onClicked: section.bridge.setPluginIndexes([section.pluginState.defaultIndex])
        }
    }

    Kirigami.PromptDialog {
        id: installDialog
        objectName: "pluginInstallDialog"
        title: section.pending.kind === "update" ? qsTr("Update Plugin?") : qsTr("Install Plugin?")
        standardButtons: Kirigami.Dialog.NoButton
        closePolicy: Controls.Popup.NoAutoClose
        customFooterActions: [
            Kirigami.Action {
                text: section.pending.kind === "update" ? qsTr("Update") : qsTr("Install")
                icon.name: "dialog-ok"
                onTriggered: section.bridge.confirmPluginInstall()
            },
            Kirigami.Action {
                text: qsTr("Cancel")
                icon.name: "dialog-cancel"
                onTriggered: section.bridge.cancelPluginInstall()
            }
        ]
        ColumnLayout {
            spacing: Kirigami.Units.largeSpacing
            Controls.Label {
                Layout.fillWidth: true
                Layout.maximumWidth: Kirigami.Units.gridUnit * 28
                wrapMode: Text.Wrap
                text: qsTr("A plugin runs as your user with access to your files and network. Install only plugins from sources you trust.")
            }
            Kirigami.FormLayout {
                Layout.fillWidth: true
                Repeater {
                    model: section.pending.rows || []
                    delegate: Controls.Label {
                        required property var modelData
                        Kirigami.FormData.label: modelData.label + ":"
                        Layout.maximumWidth: Kirigami.Units.gridUnit * 22
                        wrapMode: Text.WrapAnywhere
                        textFormat: Text.PlainText
                        text: modelData.value
                    }
                }
            }
        }
    }

    Kirigami.PromptDialog {
        id: removeDialog
        title: qsTr("Remove Plugin?")
        subtitle: qsTr("Removes the plugin's program and files. Its own settings and keyring entries stay.")
        standardButtons: Kirigami.Dialog.Cancel
        customFooterActions: [
            Kirigami.Action {
                text: qsTr("Remove")
                icon.name: "edit-delete-remove"
                onTriggered: {
                    section.bridge.removePlugin(section.removeId)
                    removeDialog.close()
                }
            }
        ]
    }
}
