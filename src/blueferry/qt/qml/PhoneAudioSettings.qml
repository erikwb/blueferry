pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

// Where the iPhone's media playback is heard: this computer or the iPhone.
// An action, not a saved preference: the switch follows the route BlueZ
// reports, and the label says where the sound actually plays.
ColumnLayout {
    id: phoneAudio
    objectName: "phoneAudioSettings"
    required property var bridge
    readonly property var info: bridge.phoneAudio || ({})

    Kirigami.Heading { text: qsTr("iPhone Sound"); level: 2 }
    Kirigami.FormLayout {
        Layout.fillWidth: true

        Controls.Switch {
            objectName: "phoneAudioSwitch"
            Layout.fillWidth: true
            text: qsTr("Play the iPhone's sound on this computer")
            checked: phoneAudio.info.onPc === true
            enabled: phoneAudio.info.available === true && phoneAudio.bridge.status.daemon === true
            onToggled: phoneAudio.bridge.setPhoneAudioRoute(checked ? "pc" : "phone")
            Accessible.description: hint.text
        }
        Controls.Label {
            id: hint
            objectName: "phoneAudioHint"
            Kirigami.FormData.label: qsTr("Status:")
            Layout.fillWidth: true
            visible: text !== ""
            wrapMode: Text.Wrap
            textFormat: Text.PlainText
            color: phoneAudio.info.mismatch === true
                ? Kirigami.Theme.neutralTextColor : Kirigami.Theme.textColor
            text: phoneAudio.info.hint || ""
        }
    }
}
