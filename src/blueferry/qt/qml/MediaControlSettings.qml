pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

// Opt-in iPhone media control over Apple Media Service (AMS).
ColumnLayout {
    id: mediaSettings
    objectName: "mediaControlSettings"
    required property var bridge
    readonly property var status: bridge.status || ({})
    readonly property bool available: status.daemon === true && !bridge.busy

    function stateText() {
        if (status.media_control_enabled !== true)
            return qsTr("Off")
        if (status.media_control_available === true)
            return qsTr("Connected to the iPhone's media service")
        return qsTr("Waiting for the iPhone's Bluetooth LE link")
    }

    Kirigami.Heading { text: qsTr("Media Control"); level: 2 }
    Kirigami.FormLayout {
        Layout.fillWidth: true

        Controls.CheckBox {
            id: enabledBox
            objectName: "mediaControlCheckBox"
            Layout.fillWidth: true
            text: qsTr("Show and control what the iPhone is playing")
            checked: mediaSettings.status.media_control_enabled === true
            enabled: mediaSettings.available
            onClicked: mediaSettings.bridge.setMediaControl(checked)
            Accessible.description: qsTr("Uses the Bluetooth LE link that also carries notifications.")
        }
        Controls.CheckBox {
            objectName: "mprisPlayerCheckBox"
            Layout.fillWidth: true
            // Only daemons that report the MPRIS preference support it.
            visible: mediaSettings.status.media_mpris_enabled !== undefined
            text: qsTr("Also show it in the desktop media controls (MPRIS)")
            checked: mediaSettings.status.media_mpris_enabled === true
            enabled: mediaSettings.available && mediaSettings.status.media_control_enabled === true
            onClicked: mediaSettings.bridge.setMprisPlayer(checked)
            Accessible.description: mprisWarning.text
        }
        Controls.Label {
            id: mprisWarning
            objectName: "mprisPlayerWarning"
            Layout.fillWidth: true
            visible: mediaSettings.status.media_mpris_enabled !== undefined
            wrapMode: Text.Wrap
            textFormat: Text.PlainText
            font: Kirigami.Theme.smallFont
            text: qsTr("Like any desktop music player, every application in your session can then "
                + "read the title, artist and album.")
        }
        Controls.Label {
            objectName: "mediaControlStateLabel"
            Kirigami.FormData.label: qsTr("Status:")
            Layout.fillWidth: true
            wrapMode: Text.Wrap
            textFormat: Text.PlainText
            text: mediaSettings.stateText()
        }
    }
}
