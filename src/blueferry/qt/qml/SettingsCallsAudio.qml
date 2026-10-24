pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

// Settings > Calls & Audio: hands-free calls, call history, missed-call
// popups, where the iPhone's sound plays, media control.
ColumnLayout {
    id: section
    objectName: "settingsCallsAudio"
    required property var bridge
    spacing: Kirigami.Units.largeSpacing

    Kirigami.Heading { text: qsTr("Calls"); level: 2 }
    // Runtime opt-in (Calls1.SetCallsEnabled); only daemons that report
    // calls_enabled support the setting.
    Loader {
        objectName: "phoneCallsLoader"
        Layout.fillWidth: true
        active: section.bridge.status.calls_enabled !== undefined
        visible: active
        sourceComponent: PhoneCallsSettings {
            bridge: section.bridge
        }
    }
    // Runtime opt-in (CallHistory1.SetCallHistory); only daemons that report
    // the call-history keys support the setting.
    Loader {
        objectName: "callHistoryLoader"
        Layout.fillWidth: true
        active: section.bridge.status.call_history_enabled !== undefined
        visible: active
        sourceComponent: CallHistorySettings {
            bridge: section.bridge
        }
    }
    Controls.CheckBox {
        objectName: "phoneBatteryWarningCheckBox"
        Layout.fillWidth: true
        // Only daemons that report the key support the setting.
        visible: section.bridge.status.phone_battery_warning !== undefined
        text: qsTr("Warn when the iPhone's battery runs low")
        checked: section.bridge.status.phone_battery_warning === true
        enabled: section.bridge.status.daemon === true && !section.bridge.busy
        onClicked: section.bridge.setPhoneBatteryWarning(checked)
        Accessible.description: qsTr("One desktop notification per discharge, at %1 % or less.")
            .arg(section.bridge.status.phone_battery_warning_percent ?? 20)
    }

    Kirigami.Heading { text: qsTr("Sound"); level: 2 }
    // Only daemons that report the audio-route keys can switch the sound.
    Loader {
        objectName: "phoneAudioLoader"
        Layout.fillWidth: true
        active: section.bridge.status.phone_audio_route !== undefined
        visible: active
        sourceComponent: PhoneAudioSettings {
            bridge: section.bridge
        }
    }
    FeatureSwitch {
        Layout.fillWidth: true
        bridge: section.bridge
        feature: "keep_phone_audio_on_phone"
        variable: "BLUEFERRY_KEEP_PHONE_AUDIO_ON_PHONE"
        text: qsTr("Keep iPhone sound on the iPhone")
        description: qsTr("On: this computer never becomes the iPhone's speaker. Turn off to allow the switch above.")
    }

    Kirigami.Heading { text: qsTr("Media"); level: 2 }
    // Runtime opt-in (Media1.SetMediaControl); only daemons that report the
    // media-control keys support the setting.
    Loader {
        objectName: "mediaControlLoader"
        Layout.fillWidth: true
        active: section.bridge.status.media_control_enabled !== undefined
        visible: active
        sourceComponent: MediaControlSettings {
            bridge: section.bridge
        }
    }
}
