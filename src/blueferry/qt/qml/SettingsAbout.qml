pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

// Settings > About: version, shortcuts, the background service, quitting.
ColumnLayout {
    id: section
    objectName: "settingsAbout"
    required property var bridge
    signal shortcutsRequested()
    signal aboutRequested()
    spacing: Kirigami.Units.largeSpacing

    Kirigami.FormLayout {
        Layout.fillWidth: true
        Controls.Label {
            Kirigami.FormData.label: qsTr("Version:")
            text: section.bridge.version || ""
            textFormat: Text.PlainText
        }
        Controls.Label {
            Kirigami.FormData.label: qsTr("Background Service:")
            text: section.bridge.status.daemon ? qsTr("Running") : qsTr("Unavailable")
        }
    }
    RowLayout {
        Controls.Button {
            objectName: "aboutButton"
            text: qsTr("About BlueFerry")
            icon.name: "help-about"
            onClicked: section.aboutRequested()
        }
        Controls.Button {
            objectName: "shortcutsButton"
            text: qsTr("Keyboard Shortcuts")
            icon.name: "preferences-desktop-keyboard-shortcuts"
            onClicked: section.shortcutsRequested()
        }
    }
    RowLayout {
        Controls.Button {
            text: qsTr("Restart Service")
            icon.name: "system-reboot"
            enabled: !section.bridge.busy
            onClicked: section.bridge.restartBackend()
        }
        Controls.Button {
            text: qsTr("Quit")
            icon.name: "application-exit"
            onClicked: Qt.quit()
        }
    }
}
