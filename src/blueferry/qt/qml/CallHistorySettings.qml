pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

// Opt-in mirror of the iPhone's recent calls. Turning it off erases the
// retained calls in the backend at once.
ColumnLayout {
    id: callHistorySettings
    objectName: "callHistorySettings"
    required property var bridge
    readonly property var status: bridge.status || ({})
    readonly property bool available: status.daemon === true && !bridge.busy
    readonly property bool enabledNow: status.call_history_enabled === true

    Kirigami.Heading { text: qsTr("Call History"); level: 2 }
    Controls.Label {
        objectName: "callHistoryPrivacyNote"
        Layout.fillWidth: true
        wrapMode: Text.Wrap
        textFormat: Text.PlainText
        text: qsTr("Keeps the iPhone's recent calls (who called and when) under your local "
            + "storage setting and can notify you about missed calls. It uses the iPhone's "
            + "Sync Contacts permission and never places, answers, or listens to calls. "
            + "Turning it off erases the retained calls.")
    }
    Kirigami.FormLayout {
        Layout.fillWidth: true

        Controls.CheckBox {
            id: enabledBox
            objectName: "callHistoryCheckBox"
            Layout.fillWidth: true
            text: qsTr("Keep the iPhone's recent calls")
            checked: callHistorySettings.enabledNow
            enabled: callHistorySettings.available
            onClicked: callHistorySettings.bridge.setCallHistory(
                checked, popupsBox.checked)
        }
        Controls.CheckBox {
            id: popupsBox
            objectName: "missedCallPopupsCheckBox"
            Layout.fillWidth: true
            text: qsTr("Notify me about missed calls")
            checked: callHistorySettings.status.missed_call_notifications !== false
            enabled: callHistorySettings.available && callHistorySettings.enabledNow
            onClicked: callHistorySettings.bridge.setCallHistory(true, checked)
        }
    }
}
