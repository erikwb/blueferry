import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

// iPhone battery (over LE, or HFP steps) and, with calls on, signal and
// network. Main.qml only loads this when
// GetStatus reports at least one value; it performs no I/O itself.
RowLayout {
    id: indicator
    required property var status
    readonly property var batteryLevel: indicator.validPercent(indicator.status.phone_battery_level)
    readonly property var signalStrength: indicator.validPercent(indicator.status.phone_signal_strength)
    readonly property string networkName: typeof indicator.status.phone_network_name === "string"
        ? indicator.status.phone_network_name : ""
    readonly property bool roaming: indicator.status.phone_network_status === "roaming"
    readonly property string summary: indicator.summaryText()
    readonly property string batteryIconName: indicator.batteryLevel !== null
        ? indicator.batteryIcon(indicator.batteryLevel) : ""
    readonly property string signalIconName: indicator.signalStrength !== null
        ? indicator.signalIcon(indicator.signalStrength) : ""
    spacing: Kirigami.Units.smallSpacing

    Accessible.role: Accessible.StaticText
    Accessible.name: indicator.summary

    function validPercent(value: var): var {
        return typeof value === "number" && value >= 0 && value <= 100 ? Math.round(value) : null
    }

    // Breeze ships battery-000 … battery-100 and network-mobile-0 … -100 in
    // 10/20 % steps; values are rounded to those, so the names always exist.
    function batteryIcon(level: int): string {
        const step = Math.max(0, Math.min(100, Math.round(level / 10) * 10))
        return "battery-" + String(step).padStart(3, "0")
    }

    function signalIcon(strength: int): string {
        const step = Math.max(0, Math.min(100, Math.round(strength / 20) * 20))
        return "network-mobile-" + step
    }

    function summaryText(): string {
        const parts = []
        if (indicator.batteryLevel !== null)
            parts.push(indicator.status.phone_battery_source === "hfp"
                ? qsTr("iPhone battery about %1 %").arg(indicator.batteryLevel)
                : qsTr("iPhone battery %1 %").arg(indicator.batteryLevel))
        if (indicator.signalStrength !== null)
            parts.push(qsTr("Signal %1 %").arg(indicator.signalStrength))
        if (indicator.networkName !== "")
            parts.push(indicator.roaming
                ? qsTr("%1 (roaming)").arg(indicator.networkName)
                : indicator.networkName)
        return parts.join(" · ")
    }

    Kirigami.Icon {
        visible: indicator.batteryIconName !== ""
        source: indicator.batteryIconName
        implicitWidth: Kirigami.Units.iconSizes.small
        implicitHeight: implicitWidth
    }
    Controls.Label {
        objectName: "phoneBatteryLabel"
        visible: indicator.batteryLevel !== null
        text: qsTr("%1 %").arg(indicator.batteryLevel)
        textFormat: Text.PlainText
        opacity: 0.8
    }
    Kirigami.Icon {
        visible: indicator.signalIconName !== ""
        source: indicator.signalIconName
        implicitWidth: Kirigami.Units.iconSizes.small
        implicitHeight: implicitWidth
    }

    HoverHandler { id: hover }

    // The operator name comes from the phone. The attached ToolTip of the
    // org.kde.desktop style renders AutoText, i.e. HTML such as "<b>…</b>";
    // an explicit PlainText label shows it literally. Replacing the style's
    // contentItem also replaces its sizing and colors, so this mirrors them:
    // wrap at 14 grid units, and use the text color of the ToolTip's own
    // color set (Tooltip, or Complementary on complementary surfaces), which
    // matches the style's background.
    Controls.ToolTip {
        id: toolTip
        objectName: "phoneStatusToolTip"
        text: indicator.summary
        visible: hover.hovered && indicator.summary !== ""
        contentItem: Item {
            implicitWidth: Math.min(toolTipLabel.maxTextWidth, toolTipLabel.contentWidth)
            implicitHeight: toolTipLabel.implicitHeight

            Controls.Label {
                id: toolTipLabel
                objectName: "phoneStatusToolTipLabel"
                readonly property real maxTextWidth: Kirigami.Units.gridUnit * 14
                text: toolTip.text
                textFormat: Text.PlainText
                wrapMode: Text.Wrap
                font: toolTip.font
                color: Kirigami.Theme.textColor
                Kirigami.Theme.colorSet: toolTip.Kirigami.Theme.colorSet
                Kirigami.Theme.inherit: false
                // Like the style: cap each line instead of binding the width
                // to the popup, which would form a binding loop.
                onLineLaidOut: line => {
                    if (line.implicitWidth > toolTipLabel.maxTextWidth)
                        line.width = toolTipLabel.maxTextWidth
                }
            }
        }
    }
}
