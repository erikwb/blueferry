pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

// Speaker and microphone for phone calls (Calls1.SetCallAudioDevice); the
// rest of the system keeps its devices. Rows and wording come from
// call_audio_view.settings_view; loaded only for daemons that report
// calls_audio_output.
ColumnLayout {
    id: callAudio
    objectName: "callAudioSettings"
    required property var bridge
    readonly property var status: callAudio.bridge.status || ({})
    readonly property var view: callAudio.bridge.callAudioSettingsView(
        callAudio.status, callAudio.bridge.callAudioDevices || ({})) || ({})
    readonly property var rows: callAudio.view.rows || []

    component DeviceCombo: Controls.ComboBox {
        id: combo
        property var row: ({})
        Layout.fillWidth: true
        model: combo.row.options || []
        textRole: "label"
        valueRole: "value"
        currentIndex: combo.row.index ?? 0
        enabled: callAudio.status.daemon === true && !callAudio.bridge.busy
        Accessible.name: combo.row.label || ""
        // Devices come and go; read them again whenever the list opens.
        popup.onAboutToShow: callAudio.bridge.loadCallAudioDevices()
        onActivated: index => {
            const choice = (combo.row.options || [])[index]
            if (choice)
                callAudio.bridge.setCallAudioDevice(combo.row.direction, choice.value)
            // Follow the saved value again once the daemon reports it.
            combo.currentIndex = Qt.binding(function() { return combo.row.index ?? 0 })
        }
    }

    Component.onCompleted: callAudio.bridge.loadCallAudioDevices()

    Kirigami.FormLayout {
        Layout.fillWidth: true
        DeviceCombo {
            objectName: "callAudioOutputCombo"
            row: callAudio.rows[0] || ({})
            Kirigami.FormData.label: row.label || ""
        }
        DeviceCombo {
            objectName: "callAudioInputCombo"
            row: callAudio.rows[1] || ({})
            Kirigami.FormData.label: row.label || ""
        }
    }
    Controls.Label {
        objectName: "callAudioSettingsHint"
        Layout.fillWidth: true
        text: callAudio.view.hint || ""
        textFormat: Text.PlainText
        wrapMode: Text.WordWrap
        opacity: 0.7
    }
}
