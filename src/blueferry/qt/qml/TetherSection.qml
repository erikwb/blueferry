pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

// Opt-in Bluetooth tethering. The daemon never starts it on its own unless
// the user configured BLUEFERRY_TETHER_AUTOCONNECT; this switch is the
// explicit action. All wording comes from the controller's shared summary.
ColumnLayout {
    id: section
    objectName: "tetherSection"
    required property var bridge

    readonly property var tether: section.bridge.tether || ({})
    readonly property bool transitioning: section.tether.pending === true
        || section.tether.state === "connecting"
        || section.tether.state === "disconnecting"

    function wantsConnection() {
        return section.tether.state === "connected" || section.tether.state === "connecting"
    }

    Layout.fillWidth: true
    spacing: Kirigami.Units.smallSpacing

    Kirigami.Heading { text: qsTr("Internet Sharing"); level: 2 }
    Controls.Label {
        Layout.fillWidth: true
        wrapMode: Text.Wrap
        text: qsTr("Use the iPhone's Personal Hotspot over Bluetooth. Turn on Personal Hotspot on the iPhone first. BlueFerry connects only when you switch this on.")
    }
    Controls.Switch {
        id: tetherSwitch
        objectName: "tetherSwitch"
        text: qsTr("Share iPhone Internet")
        checked: section.wantsConnection()
        enabled: section.bridge.status.daemon === true && !section.transitioning
        onToggled: {
            section.bridge.setTetherEnabled(checked)
            // Reflect the daemon's state, not the click, until it reports back.
            checked = Qt.binding(function() { return section.wantsConnection() })
        }
    }
    Controls.Label {
        objectName: "tetherSummary"
        Layout.fillWidth: true
        visible: section.tether.state !== "failed"
        wrapMode: Text.Wrap
        textFormat: Text.PlainText
        text: section.tether.summary || ""
    }
    Kirigami.InlineMessage {
        objectName: "tetherError"
        Layout.fillWidth: true
        visible: section.tether.state === "failed"
        type: Kirigami.MessageType.Warning
        text: section.tether.summary || ""
    }
}
