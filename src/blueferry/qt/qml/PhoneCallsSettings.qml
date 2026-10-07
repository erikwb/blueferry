pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

// Opt-in for the experimental phone-call integration through oFono. Off by
// default; the daemon saves the choice and applies it without a restart.
ColumnLayout {
    id: callsSettings
    objectName: "phoneCallsSettings"
    required property var bridge
    readonly property var status: bridge.status || ({})
    readonly property bool available: status.daemon === true && !bridge.busy

    Kirigami.Heading { text: qsTr("Phone Calls"); level: 2 }
    Kirigami.InlineMessage {
        objectName: "phoneCallsNotice"
        Layout.fillWidth: true
        visible: true
        type: Kirigami.MessageType.Information
        text: qsTr("Experimental. Needs oFono. While this is on, the iPhone's hands-free link "
            + "stays connected to this computer, so calls can ring and be answered here and "
            + "their audio plays here. Music stays on the iPhone. Emergency numbers are always "
            + "dialed on the iPhone itself.")
    }
    Kirigami.FormLayout {
        Layout.fillWidth: true

        Controls.CheckBox {
            objectName: "phoneCallsCheckBox"
            Layout.fillWidth: true
            text: qsTr("Enable phone calls through this computer")
            checked: callsSettings.status.calls_enabled === true
            enabled: callsSettings.available
            onClicked: callsSettings.bridge.setCallsEnabled(checked)
        }
    }
}
