pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

// Per-app rules for what clicking a mirrored iPhone notification opens.
// The backend validates every rule; this view only collects and lists them.
ColumnLayout {
    id: editor
    objectName: "notificationOpenMapEditor"
    required property var bridge
    spacing: Kirigami.Units.smallSpacing

    readonly property var rules: editor.bridge.notificationOpenMap || []
    readonly property string bundleDraft: bundleField.text.trim()
    readonly property string targetDraft: targetField.text.trim()

    function canAdd() {
        return editor.bundleDraft !== ""
            && editor.targetDraft !== ""
            && editor.bridge.status.daemon === true
            && !editor.bridge.busy
    }

    function add() {
        if (!editor.canAdd())
            return
        editor.bridge.setNotificationOpenTarget(editor.bundleDraft, editor.targetDraft)
    }

    Component.onCompleted: editor.bridge.loadNotificationOpenMap()

    Connections {
        target: editor.bridge
        // Rule edits from the CLI or another client emit the backend's
        // content-free StatusChanged; reread the rules when status refreshes.
        function onStatusChanged() {
            editor.bridge.loadNotificationOpenMap()
        }
        function onNotificationOpenMapChanged() {
            // Clear the form only once the backend has accepted this rule.
            for (let index = 0; index < editor.rules.length; ++index) {
                const rule = editor.rules[index]
                if (rule.bundle_id === editor.bundleDraft && rule.target === editor.targetDraft) {
                    bundleField.clear()
                    targetField.clear()
                    return
                }
            }
        }
    }

    Kirigami.Heading {
        text: qsTr("Clicking iPhone Notifications")
        level: 3
    }
    Controls.Label {
        Layout.fillWidth: true
        wrapMode: Text.Wrap
        text: qsTr("Choose a web address (http or https) or a desktop app to open when a notification from an iPhone app is clicked. Apps without a rule keep the default behaviour. Notification text is never passed to the app or address.")
    }

    Repeater {
        model: editor.rules
        delegate: RowLayout {
            id: ruleRow
            required property var modelData
            Layout.fillWidth: true

            Controls.Label {
                text: ruleRow.modelData.bundle_id
                textFormat: Text.PlainText
                elide: Text.ElideMiddle
                Layout.maximumWidth: Kirigami.Units.gridUnit * 14
            }
            Controls.Label {
                Layout.fillWidth: true
                text: ruleRow.modelData.target
                textFormat: Text.PlainText
                elide: Text.ElideMiddle
            }
            Controls.Button {
                objectName: "removeOpenRule"
                icon.name: "list-remove"
                text: qsTr("Remove")
                display: Controls.AbstractButton.IconOnly
                enabled: editor.bridge.status.daemon === true && !editor.bridge.busy
                Accessible.name: qsTr("Remove rule for %1").arg(ruleRow.modelData.bundle_id)
                Controls.ToolTip.text: text
                Controls.ToolTip.visible: hovered
                onClicked: editor.bridge.removeNotificationOpenTarget(ruleRow.modelData.bundle_id)
            }
        }
    }
    Controls.Label {
        visible: editor.rules.length === 0
        Layout.fillWidth: true
        wrapMode: Text.Wrap
        opacity: 0.7
        text: qsTr("No rules yet.")
    }

    RowLayout {
        Layout.fillWidth: true

        Controls.TextField {
            id: bundleField
            objectName: "openRuleBundleField"
            Layout.fillWidth: true
            maximumLength: 255
            inputMethodHints: Qt.ImhNoAutoUppercase | Qt.ImhNoPredictiveText
            placeholderText: qsTr("iPhone app, e.g. com.apple.mobilemail")
            Accessible.name: qsTr("iPhone app bundle ID")
            onAccepted: targetField.forceActiveFocus()
        }
        Controls.TextField {
            id: targetField
            objectName: "openRuleTargetField"
            Layout.fillWidth: true
            maximumLength: 2048
            inputMethodHints: Qt.ImhNoAutoUppercase | Qt.ImhNoPredictiveText | Qt.ImhUrlCharactersOnly
            placeholderText: qsTr("https://… or org.mozilla.Thunderbird.desktop")
            Accessible.name: qsTr("Web address or desktop app ID to open")
            onAccepted: editor.add()
        }
        Controls.Button {
            objectName: "addOpenRule"
            icon.name: "list-add"
            text: qsTr("Add")
            enabled: editor.canAdd()
            onClicked: editor.add()
        }
    }
}
