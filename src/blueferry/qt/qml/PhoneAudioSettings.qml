import QtQuick

// Where the iPhone's media playback is heard: this computer or the iPhone.
// The switch follows the route BlueZ reports; the backend remembers an
// explicit choice for reconnects. The subtitle says why it is unavailable
// or where the sound actually plays.
SubtitleSwitch {
    id: phoneAudio
    objectName: "phoneAudioSwitch"
    required property var bridge
    readonly property var info: phoneAudio.bridge.phoneAudio || ({})

    horizontalPadding: 0
    text: qsTr("Play the iPhone's sound on this computer")
    subtitle: phoneAudio.info.hint || ""
    checked: phoneAudio.info.onPc === true
    enabled: phoneAudio.info.available === true && phoneAudio.bridge.status.daemon === true
    onToggled: {
        phoneAudio.bridge.setPhoneAudioRoute(checked ? "pc" : "phone")
        checked = Qt.binding(function() { return phoneAudio.info.onPc === true })
    }
}
