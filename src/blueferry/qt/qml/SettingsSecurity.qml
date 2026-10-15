pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

// Settings > Security: lock when away, local storage and its encryption,
// clearing the history.
ColumnLayout {
    id: section
    objectName: "settingsSecurity"
    required property var bridge
    signal storagePolicyRequested(string policy)
    signal clearHistoryRequested()
    spacing: Kirigami.Units.largeSpacing

    function storageStatusText() {
        const status = section.bridge.status || ({})
        if (status.daemon !== true)
            return qsTr("Unavailable")
        if (status.storage_policy === "none")
            return qsTr("Disabled")
        if (status.storage_state === "ready")
            return qsTr("Available")
        if (status.storage_state === "locked")
            return qsTr("Locked")
        return qsTr("Unavailable")
    }

    // Only daemons that report the proximity keys support the setting.
    Loader {
        objectName: "proximityLockLoader"
        Layout.fillWidth: true
        active: section.bridge.status.proximity_lock !== undefined
        visible: active
        sourceComponent: ProximityLockSettings {
            bridge: section.bridge
        }
    }

    Kirigami.Heading { text: qsTr("Local Data"); level: 2 }
    Kirigami.FormLayout {
        Layout.fillWidth: true

        Controls.ComboBox {
            objectName: "storagePolicySelector"
            Kirigami.FormData.label: qsTr("Storage:")
            textRole: "text"
            valueRole: "value"
            model: [
                { "text": qsTr("Encrypted with Desktop Keyring"), "value": "encrypted" },
                { "text": qsTr("Unencrypted Local Data"), "value": "plaintext" },
                { "text": qsTr("Do Not Retain Local Data"), "value": "none" }
            ]
            currentIndex: section.bridge.status.storage_policy === "plaintext" ? 1
                : section.bridge.status.storage_policy === "none" ? 2 : 0
            enabled: section.bridge.status.daemon === true && !section.bridge.busy
            onActivated: {
                section.storagePolicyRequested(currentValue)
            }
        }

        Controls.Label {
            objectName: "storageStatusLabel"
            Kirigami.FormData.label: qsTr("Status:")
            Layout.fillWidth: true
            text: section.storageStatusText()
            textFormat: Text.PlainText
        }

        Controls.Label {
            Kirigami.FormData.label: qsTr("Details:")
            Layout.fillWidth: true
            visible: section.bridge.status.storage_detail !== undefined
                && section.bridge.status.storage_detail !== ""
            text: section.bridge.status.storage_detail || ""
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
        }

        Controls.Button {
            Kirigami.FormData.label: qsTr("Keyring:")
            visible: section.bridge.status.storage_policy === "encrypted"
                && section.bridge.status.storage_state !== "ready"
            text: qsTr("Unlock Local Data")
            icon.name: "document-decrypt"
            enabled: section.bridge.status.daemon === true && !section.bridge.busy
            onClicked: section.bridge.unlockStorage()
        }
    }

    RowLayout {
        Controls.Button {
            text: qsTr("Clear History")
            icon.name: "edit-clear-history"
            enabled: !section.bridge.busy
            onClicked: section.clearHistoryRequested()
        }
    }
}
