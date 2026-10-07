pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

// Opt-in "lock when my iPhone goes away" preference. Lock only: BlueFerry
// never unlocks the desktop, because Bluetooth presence can be spoofed.
ColumnLayout {
    id: proximitySettings
    objectName: "proximityLockSettings"
    required property var bridge
    readonly property var status: bridge.status || ({})
    readonly property bool available: status.daemon === true && !bridge.busy

    function stateText() {
        switch (status.proximity_lock) {
        case "armed":
            return qsTr("Armed: your iPhone is connected")
        case "grace":
            return qsTr("iPhone disconnected; locking in %1 s unless it reconnects")
                .arg(status.proximity_lock_remaining_sec || 0)
        case "locked":
            return qsTr("Locked the desktop; waiting for your iPhone to return")
        case "idle":
            return status.proximity_lock_inhibited
                ? qsTr("Paused (Bluetooth off, discovery, suspend, recovery, or disconnected from this computer)")
                : qsTr("Waiting to see your iPhone connected")
        default:
            return qsTr("Off")
        }
    }

    function save() {
        proximitySettings.bridge.setProximityLock(enabledBox.checked, graceBox.value)
    }

    Kirigami.Heading { text: qsTr("Away Lock"); level: 2 }
    Kirigami.InlineMessage {
        objectName: "proximityLockWarning"
        Layout.fillWidth: true
        visible: true
        type: Kirigami.MessageType.Warning
        text: qsTr("Locks this desktop after your iPhone has been disconnected for the chosen time. "
            + "This is a convenience, not a security feature: Bluetooth presence can be relayed or "
            + "spoofed, and a phone that turns off Bluetooth also triggers it. BlueFerry never "
            + "unlocks the desktop when the iPhone returns.")
    }
    Kirigami.FormLayout {
        Layout.fillWidth: true

        Controls.CheckBox {
            id: enabledBox
            objectName: "proximityLockCheckBox"
            Layout.fillWidth: true
            text: qsTr("Lock the desktop when my iPhone goes away")
            checked: proximitySettings.status.proximity_lock_enabled === true
            enabled: proximitySettings.available
            onClicked: proximitySettings.save()
        }
        Controls.SpinBox {
            id: graceBox
            objectName: "proximityLockGraceSpinBox"
            Kirigami.FormData.label: qsTr("Lock after (seconds):")
            from: 10
            to: 3600
            stepSize: 10
            editable: true
            value: 60
            enabled: proximitySettings.available
            // Coalesce spin clicks into one settings call.
            onValueModified: saveTimer.restart()
        }
        Controls.Label {
            objectName: "proximityLockStateLabel"
            Kirigami.FormData.label: qsTr("Status:")
            Layout.fillWidth: true
            wrapMode: Text.Wrap
            textFormat: Text.PlainText
            text: proximitySettings.stateText()
        }
    }
    // Follow the daemon's value, except while an edit is waiting to be saved:
    // a StatusChanged refresh in that window must not undo the user's input.
    Binding {
        target: graceBox
        property: "value"
        value: proximitySettings.status.proximity_lock_grace_sec || 60
        when: !saveTimer.running
        restoreMode: Binding.RestoreNone
    }
    Timer {
        id: saveTimer
        interval: 800
        repeat: false
        onTriggered: proximitySettings.save()
    }
}
