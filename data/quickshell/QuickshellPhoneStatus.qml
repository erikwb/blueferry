pragma ComponentBehavior: Bound

import QtQuick

// Optional iPhone battery (over Bluetooth LE, or in 20 % steps from HFP) and
// signal (HFP, only with calls on). Hidden unless the backend status carries
// a value; the operator name stays out of the compact header. English and
// upper case like the rest of this shell, which loads no translation catalog
// (po/README.md).
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
    if (root.batteryLevel !== null) {
      // "~" marks a 20 % hands-free step without adding English text.
      const about = root.status.phone_battery_source === "hfp" ? "~" : ""
      parts.push("BATTERY " + about + root.batteryLevel + " %")
    }
    if (root.signalStrength !== null) parts.push("SIGNAL " + root.signalStrength + " %")
    return parts.join(" · ")
  }

  visible: root.summary !== ""
  text: root.summary
  color: root.ferryTheme.muted
  font.pixelSize: root.ferryTheme.captionSize
}
