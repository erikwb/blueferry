pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

// Opt-in for copying one-time codes to the clipboard. Off by default; the
// daemon saves the choice and applies it without a restart.
ColumnLayout {
    id: otpSettings
    objectName: "otpAutocopySettings"
    required property var bridge
    readonly property var status: bridge.status || ({})
    readonly property bool available: status.daemon === true && !bridge.busy

    Kirigami.Heading { text: qsTr("One-Time Codes"); level: 2 }
    Kirigami.FormLayout {
        Layout.fillWidth: true

        Controls.CheckBox {
            objectName: "otpAutocopyCheckBox"
            Layout.fillWidth: true
            text: qsTr("Copy one-time codes to the clipboard")
            checked: otpSettings.status.otp_autocopy === true
            enabled: otpSettings.available
            onClicked: otpSettings.bridge.setOtpAutocopy(checked)
            Accessible.description: otpCaption.text
        }
        Controls.Label {
            id: otpCaption
            objectName: "otpAutocopyCaption"
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            font: Kirigami.Theme.smallFont
            text: qsTr("Only new messages from senders that are not in your contacts, never "
                + "group chats. Codes are not stored or logged. Turning this off clears a "
                + "copied code that is still on the clipboard.")
        }
    }
}
