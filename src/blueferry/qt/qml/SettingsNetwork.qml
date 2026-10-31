pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

ColumnLayout {
    id: networkPage
    objectName: "settingsNetwork"
    required property var bridge

    spacing: Kirigami.Units.largeSpacing

    Loader {
        id: tetherLoader
        objectName: "tetherLoader"
        Layout.fillWidth: true
        active: (networkPage.bridge.tether || ({})).available === true
        visible: active
        sourceComponent: Component {
            TetherSection { bridge: networkPage.bridge }
        }
    }
    Controls.Label {
        objectName: "tetherUnavailable"
        Layout.fillWidth: true
        visible: !tetherLoader.active
        text: qsTr("Bluetooth tethering is unavailable.")
        wrapMode: Text.Wrap
    }
}
