pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

// Opt-in Bluetooth tethering. Nothing happens until the user ticks "Enable
// Bluetooth tethering": until then the daemon ignores Bluetooth network
// links entirely. Once enabled, the switch is the explicit connect action
// and automatic tethering is a separate choice. All state wording comes from
// the controller's shared summary.
ColumnLayout {
    id: section
    objectName: "tetherSection"
    required property var bridge

    readonly property var tether: section.bridge.tether || ({})
    readonly property bool featureEnabled: section.tether.enabled === true
    readonly property bool daemonReady: section.bridge.status.daemon === true
    readonly property bool transitioning: section.tether.pending === true
        || section.tether.state === "connecting"
        || section.tether.state === "disconnecting"

    function wantsConnection() {
        return section.tether.state === "connected" || section.tether.state === "connecting"
    }

    Layout.fillWidth: true
    spacing: Kirigami.Units.smallSpacing

    Kirigami.Heading { text: qsTr("Internet Sharing"); level: 2 }
    Controls.CheckBox {
        id: enableBox
        objectName: "tetherEnableCheckBox"
        Layout.fillWidth: true
        text: qsTr("Enable Bluetooth tethering")
        checked: section.featureEnabled
        enabled: section.daemonReady && section.tether.pending !== true
        onClicked: {
            section.bridge.setTethering(checked, section.tether.autoconnect === true)
            // Reflect the saved setting, not the click, until the daemon answers.
            checked = Qt.binding(function() { return section.featureEnabled })
        }
        Accessible.description: qsTr("While this is off, BlueFerry ignores Bluetooth network connections, including ones started from the network applet.")
    }
    Controls.Label {
        Layout.fillWidth: true
        wrapMode: Text.Wrap
        text: section.featureEnabled
            ? qsTr("Use the iPhone's Personal Hotspot over Bluetooth. Turn on Personal Hotspot on the iPhone first. BlueFerry connects only when you switch this on.")
            : qsTr("Lets BlueFerry use the iPhone's Personal Hotspot over Bluetooth. While this is off, BlueFerry leaves Bluetooth network connections alone, including ones started from the network applet.")
    }
    Controls.Switch {
        id: tetherSwitch
        objectName: "tetherSwitch"
        visible: section.featureEnabled
        text: qsTr("Share iPhone Internet")
        checked: section.wantsConnection()
        enabled: section.daemonReady && !section.transitioning
        onToggled: {
            section.bridge.setTetherConnected(checked)
            // Reflect the daemon's state, not the click, until it reports back.
            checked = Qt.binding(function() { return section.wantsConnection() })
        }
    }
    Controls.CheckBox {
        id: autoBox
        objectName: "tetherAutoconnectCheckBox"
        Layout.fillWidth: true
        visible: section.featureEnabled
        text: qsTr("Connect automatically when the iPhone is connected")
        checked: section.tether.autoconnect === true
        enabled: section.daemonReady && section.tether.pending !== true
        onClicked: {
            section.bridge.setTethering(true, checked)
            checked = Qt.binding(function() { return section.tether.autoconnect === true })
        }
    }
    Controls.Label {
        objectName: "tetherSummary"
        Layout.fillWidth: true
        visible: section.featureEnabled && section.tether.state !== "failed"
        wrapMode: Text.Wrap
        textFormat: Text.PlainText
        text: section.tether.summary || ""
    }
    Kirigami.InlineMessage {
        objectName: "tetherError"
        Layout.fillWidth: true
        visible: section.featureEnabled && section.tether.state === "failed"
        type: Kirigami.MessageType.Warning
        text: section.tether.summary || ""
    }
}
