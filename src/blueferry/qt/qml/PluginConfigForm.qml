pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import "ui"

// A settings form generated from a plugin's [Config …] schema (PLUGINS.md,
// ApiVersion 1.3): sections ("Advanced" folded), placeholders, examples, a
// "Where do I find this?" link per field, inline errors from the client's
// pre-check (bridge.checkPluginConfig) and from the plugin, "Test
// connection", a browser sign-in and one status line. A stored secret is
// never shown: its field stays empty ("leave empty to keep") and only a newly
// typed value is sent. Save stays off until the visible required fields are
// filled and valid. Everything a plugin says is plain text.
ColumnLayout {
    id: form
    objectName: "pluginConfigForm"
    required property var bridge
    required property var config
    property bool busy: false
    property var draft: ({})
    // {errors, visible, valid} from the bridge; recomputed on every edit.
    property var check: ({errors: {}, visible: [], valid: false})
    // Field definitions, replaced only when they really change, so a status
    // update does not rebuild the fields under the user's cursor.
    property var fields: []
    property string fieldsKey: ""
    // Keys edited since the plugin last answered: its old reason is stale.
    property var edited: ({})
    property string serverErrorsKey: ""
    property var revealed: ({})
    property var expanded: ({})
    readonly property var groups: form.config.groups || [{name: "", label: "", help: "", collapsed: false}]
    readonly property var actions: form.config.actions || ({})
    readonly property var status: form.config.status || ({})
    readonly property var serverErrors: form.config.errors || ({})
    readonly property bool ready: form.config.loaded === true && !form.busy
    // Width of the label column: the widest label, at most 40 % of the form.
    readonly property real labelWidth: Math.min(form.naturalLabelWidth, form.width * 0.4)
    readonly property real naturalLabelWidth: {
        let widest = 0
        for (const field of form.fields)
            widest = Math.max(widest, labelMetrics.advanceWidth(form.labelText(field)))
        return Math.ceil(widest) + Kirigami.Units.smallSpacing
    }
    // Labels beside the fields while there is room for a usable field next
    // to them; above the fields otherwise. Same choice for every section.
    readonly property bool wide: form.width >= form.labelWidth + Kirigami.Units.gridUnit * 20
    spacing: Kirigami.Units.smallSpacing

    FontMetrics {
        id: labelMetrics
        font: Kirigami.Theme.defaultFont
    }

    function labelText(field) {
        return field.label + (field.required ? " *" : "") + ":"
    }
    // A placeholder is always an example, never a value: "e.g. …", so a
    // grey "192.168.1.20" is not mistaken for the current setting.
    function placeholderText(text) {
        return text === "" ? "" : qsTr("e.g. %1").arg(text)
    }
    function recheck() {
        if (!form.config.id || form.config.loaded !== true)
            return
        const result = form.bridge.checkPluginConfig(form.config.id, form.draft)
        form.check = result || ({errors: {}, visible: [], valid: false})
    }
    function set(key, value) {
        const next = Object.assign({}, form.draft)
        next[key] = value
        form.draft = next
        form.edited = form.flag(form.edited, key, true)
        form.recheck()
    }
    function flag(map, key, value) {
        const next = Object.assign({}, map)
        next[key] = value
        return next
    }
    function valueOf(field) {
        return Object.prototype.hasOwnProperty.call(form.draft, field.key) ? form.draft[field.key] : field.value
    }
    function shown(key) {
        return (form.check.visible || []).indexOf(key) >= 0
    }
    function errorOf(key) {
        return (form.check.errors || ({}))[key] || (form.edited[key] === true ? "" : form.serverErrors[key] || "")
    }
    function groupHasError(name) {
        return form.fields.some(field => (field.group || "") === name && form.errorOf(field.key) !== "")
    }
    function isOpen(group) {
        if (Object.prototype.hasOwnProperty.call(form.expanded, group.name))
            return form.expanded[group.name]
        return !group.collapsed || form.groupHasError(group.name)
    }
    function syncFields() {
        const key = JSON.stringify([form.config.id, form.config.fields || []])
        if (key !== form.fieldsKey) {
            form.fieldsKey = key
            form.fields = form.config.fields || []
        }
        const errorsKey = JSON.stringify(form.serverErrors)
        if (errorsKey !== form.serverErrorsKey) {
            form.serverErrorsKey = errorsKey
            form.edited = ({})
        }
        form.recheck()
    }

    onConfigChanged: form.syncFields()
    Component.onCompleted: form.syncFields()

    Controls.Label {
        visible: form.config.loaded !== true
        text: qsTr("Loading the plugin's settings…")
    }
    Notice {
        objectName: "pluginConfigFormError"
        type: Kirigami.MessageType.Error
        plainText: form.serverErrors[""] || ""
    }

    Repeater {
        model: form.groups
        delegate: ColumnLayout {
            id: section
            required property var modelData
            required property int index
            readonly property bool open: form.isOpen(section.modelData)
            readonly property var members: form.fields.filter(field => (field.group || "") === (section.modelData.name || ""))
            objectName: "configGroup_" + (section.modelData.name || "main")
            Layout.fillWidth: true
            visible: section.members.length > 0
            spacing: Kirigami.Units.smallSpacing

            Kirigami.Separator {
                Layout.fillWidth: true
                visible: section.index > 0
            }
            SectionHeader {
                visible: section.modelData.label !== ""
                text: section.modelData.label
                level: 4
                Controls.ToolButton {
                    objectName: "configGroupToggle"
                    visible: section.modelData.collapsed === true
                    icon.name: section.open ? "arrow-up" : "arrow-down"
                    text: section.open ? qsTr("Hide") : qsTr("Show")
                    display: Controls.AbstractButton.TextBesideIcon
                    onClicked: form.expanded = form.flag(form.expanded, section.modelData.name, !section.open)
                }
            }
            Controls.Label {
                Layout.fillWidth: true
                visible: (section.modelData.help || "") !== "" && section.open
                wrapMode: Text.Wrap
                textFormat: Text.PlainText
                color: Kirigami.Theme.disabledTextColor
                text: section.modelData.help || ""
            }

            // Every section uses the same two columns (labels right-aligned
            // beside the fields) or, in a narrow form, labels above the
            // fields: one rule for all sections, folded ones included.
            ColumnLayout {
                Layout.fillWidth: true
                visible: section.open
                enabled: form.ready
                spacing: Kirigami.Units.largeSpacing

                Repeater {
                    model: section.members
                    delegate: GridLayout {
                        id: fieldRow
                        required property var modelData
                        readonly property string key: fieldRow.modelData.key
                        readonly property string type: fieldRow.modelData.type
                        readonly property string error: form.errorOf(fieldRow.key)
                        readonly property bool isText: ["string", "url", "secret"].indexOf(fieldRow.type) >= 0
                        objectName: "configRow_" + fieldRow.key
                        Layout.fillWidth: true
                        visible: form.shown(fieldRow.key)
                        columns: form.wide ? 2 : 1
                        columnSpacing: Kirigami.Units.largeSpacing
                        rowSpacing: Kirigami.Units.smallSpacing

                        Controls.Label {
                            objectName: "configLabel_" + fieldRow.key
                            Layout.fillWidth: !form.wide
                            Layout.preferredWidth: form.wide ? form.labelWidth : -1
                            Layout.maximumWidth: form.wide ? form.labelWidth : Number.POSITIVE_INFINITY
                            Layout.alignment: Qt.AlignTop | (form.wide ? Qt.AlignRight : Qt.AlignLeft)
                            Layout.topMargin: form.wide ? Math.max(0, (controls.height - implicitHeight) / 2) : 0
                            horizontalAlignment: form.wide ? Text.AlignRight : Text.AlignLeft
                            wrapMode: Text.Wrap
                            textFormat: Text.PlainText
                            text: form.labelText(fieldRow.modelData)
                        }
                        ColumnLayout {
                            Layout.fillWidth: true
                            Layout.minimumWidth: 0
                            Layout.preferredWidth: 0
                            spacing: Kirigami.Units.smallSpacing / 2

                            RowLayout {
                                id: controls
                                Layout.fillWidth: true
                                spacing: 0
                                Controls.TextField {
                                    id: textField
                                    objectName: "configField_" + fieldRow.key
                                    visible: fieldRow.isText
                                    Layout.fillWidth: true
                                    Layout.minimumWidth: Kirigami.Units.gridUnit * 6
                                    Layout.maximumWidth: Kirigami.Units.gridUnit * 30
                                    echoMode: fieldRow.type === "secret" && form.revealed[fieldRow.key] !== true
                                        ? TextInput.Password : TextInput.Normal
                                    inputMethodHints: fieldRow.type === "url" ? Qt.ImhUrlCharactersOnly
                                        : (fieldRow.type === "secret" ? Qt.ImhSensitiveData | Qt.ImhNoPredictiveText : Qt.ImhNone)
                                    text: fieldRow.isText ? String(form.valueOf(fieldRow.modelData) || "") : ""
                                    placeholderText: fieldRow.type === "secret" && fieldRow.modelData.stored
                                        ? qsTr("Stored — leave empty to keep")
                                        : form.placeholderText(fieldRow.modelData.placeholder || "")
                                    Accessible.name: fieldRow.modelData.label
                                    Accessible.description: fieldRow.modelData.help || ""
                                    onTextEdited: form.set(fieldRow.key, text)
                                }
                                Controls.ToolButton {
                                    objectName: "configReveal_" + fieldRow.key
                                    visible: fieldRow.type === "secret"
                                    icon.name: form.revealed[fieldRow.key] === true ? "password-show-off" : "password-show-on"
                                    text: form.revealed[fieldRow.key] === true ? qsTr("Hide") : qsTr("Show")
                                    display: Controls.AbstractButton.IconOnly
                                    Controls.ToolTip.text: text
                                    Controls.ToolTip.visible: hovered
                                    onClicked: form.revealed = form.flag(form.revealed, fieldRow.key, form.revealed[fieldRow.key] !== true)
                                }
                                Controls.Switch {
                                    objectName: "configSwitch_" + fieldRow.key
                                    visible: fieldRow.type === "bool"
                                    checked: form.valueOf(fieldRow.modelData) === true
                                    Accessible.name: fieldRow.modelData.label
                                    onToggled: form.set(fieldRow.key, checked)
                                }
                                Controls.SpinBox {
                                    visible: fieldRow.type === "int"
                                    from: fieldRow.modelData.minimum
                                    to: fieldRow.modelData.maximum
                                    editable: true
                                    value: fieldRow.type === "int" ? Number(form.valueOf(fieldRow.modelData)) : 0
                                    Accessible.name: fieldRow.modelData.label
                                    onValueModified: form.set(fieldRow.key, value)
                                }
                                Controls.ComboBox {
                                    visible: fieldRow.type === "choice"
                                    Layout.maximumWidth: Kirigami.Units.gridUnit * 20
                                    model: fieldRow.modelData.choices || []
                                    currentIndex: Math.max(0, (fieldRow.modelData.choices || []).indexOf(form.valueOf(fieldRow.modelData)))
                                    Accessible.name: fieldRow.modelData.label
                                    onActivated: form.set(fieldRow.key, currentText)
                                }
                                Item {
                                    Layout.fillWidth: !fieldRow.isText
                                }
                            }
                            Controls.Label {
                                objectName: "configError_" + fieldRow.key
                                Layout.fillWidth: true
                                visible: fieldRow.error !== ""
                                wrapMode: Text.Wrap
                                textFormat: Text.PlainText
                                color: Kirigami.Theme.negativeTextColor
                                text: fieldRow.error
                            }
                            Controls.Label {
                                objectName: "configHelpText_" + fieldRow.key
                                Layout.fillWidth: true
                                visible: (fieldRow.modelData.help || "") !== ""
                                wrapMode: Text.Wrap
                                textFormat: Text.PlainText
                                font: Kirigami.Theme.smallFont
                                color: Kirigami.Theme.disabledTextColor
                                text: fieldRow.modelData.help || ""
                            }
                            Controls.Label {
                                objectName: "configExample_" + fieldRow.key
                                Layout.fillWidth: true
                                visible: (fieldRow.modelData.example || "") !== ""
                                wrapMode: Text.Wrap
                                textFormat: Text.PlainText
                                font: Kirigami.Theme.smallFont
                                color: Kirigami.Theme.disabledTextColor
                                text: qsTr("Example: %1").arg(fieldRow.modelData.example || "")
                            }
                            Controls.ToolButton {
                                objectName: "configHelp_" + fieldRow.key
                                Layout.maximumWidth: parent.width
                                visible: (fieldRow.modelData.helpUrl || "") !== ""
                                icon.name: "help-contextual"
                                text: qsTr("More information")
                                display: Controls.AbstractButton.TextBesideIcon
                                font: Kirigami.Theme.smallFont
                                // Tooltips detect rich text: the escaped form of the URL.
                                Controls.ToolTip.text: fieldRow.modelData.helpUrlText || ""
                                Controls.ToolTip.visible: hovered
                                onClicked: form.bridge.openPluginHelp(form.config.id, fieldRow.key)
                            }
                        }
                    }
                }
            }
        }
    }

    RowLayout {
        Layout.fillWidth: true
        visible: (form.status.text || "") !== ""
        spacing: Kirigami.Units.smallSpacing
        Controls.BusyIndicator {
            running: form.status.pending === true
            visible: running
            Layout.preferredHeight: Kirigami.Units.iconSizes.smallMedium
            Layout.preferredWidth: Kirigami.Units.iconSizes.smallMedium
        }
        Notice {
            objectName: "pluginConfigStatus"
            type: form.status.pending === true ? Kirigami.MessageType.Information
                : (form.status.ok === true ? Kirigami.MessageType.Positive : Kirigami.MessageType.Error)
            plainText: form.status.text || ""
        }
    }

    Flow {
        Layout.fillWidth: true
        spacing: Kirigami.Units.smallSpacing
        Controls.Button {
            objectName: "pluginConfigSave"
            text: qsTr("Save")
            icon.name: "document-save"
            enabled: form.ready && form.check.valid === true && Object.keys(form.draft).length > 0
            onClicked: form.bridge.savePluginConfig(form.config.id, form.draft)
        }
        Controls.Button {
            objectName: "pluginConfigTest"
            visible: form.actions.test === true
            text: qsTr("Test Connection")
            icon.name: "network-connect"
            enabled: form.ready && form.check.valid === true && form.status.pending !== true
            onClicked: form.bridge.testPluginConfig(form.config.id, form.draft)
        }
        Controls.Button {
            objectName: "pluginConfigSignIn"
            visible: (form.actions.loginLabel || "") !== "" && !(form.status.kind === "login" && form.status.pending === true)
            text: form.actions.loginLabel || ""
            icon.name: "internet-services"
            enabled: form.ready
            onClicked: form.bridge.signInPlugin(form.config.id, form.draft)
        }
        Controls.Button {
            objectName: "pluginConfigCancelSignIn"
            visible: form.status.kind === "login" && form.status.pending === true
            text: qsTr("Cancel Sign-in")
            icon.name: "dialog-cancel"
            onClicked: form.bridge.cancelPluginSignIn()
        }
    }
    Controls.Label {
        Layout.fillWidth: true
        visible: form.ready && form.check.valid !== true
            && Object.keys(form.check.errors || ({})).length === 0
        wrapMode: Text.Wrap
        font: Kirigami.Theme.smallFont
        color: Kirigami.Theme.disabledTextColor
        text: qsTr("Fill in the fields marked * to save.")
    }
}
