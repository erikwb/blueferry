pragma ComponentBehavior: Bound

import QtQuick

// Optional iPhone battery and signal from the HFP calls integration
// (BLUEFERRY_CALLS_ENABLED=true with oFono). Hidden unless the backend
// status carries a value; the operator name stays out of the compact header.
FerryLabel {
  id: root

  required property var status

  readonly property var batteryLevel: root.validPercent(root.status.phone_battery_level)
  readonly property var signalStrength: root.validPercent(root.status.phone_signal_strength)
  readonly property string summary: root.summaryText()

  function validPercent(value: var): var {
    return typeof value === "number" && value >= 0 && value <= 100 ? Math.round(value) : null
  }

  function summaryText(): string {
    const parts = []
    if (root.batteryLevel !== null) parts.push("BATTERY " + root.batteryLevel + " %")
    if (root.signalStrength !== null) parts.push("SIGNAL " + root.signalStrength + " %")
    return parts.join(" · ")
  }

  visible: root.summary !== ""
  text: root.summary
  color: root.ferryTheme.muted
  font.pixelSize: root.ferryTheme.captionSize
}
