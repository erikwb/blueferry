pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls

CheckBox {
  id: control
  required property var ferryTheme
  spacing: control.ferryTheme.scaled(8)
  implicitHeight: Math.max(control.ferryTheme.scaled(24), contentItem.implicitHeight)
  indicator: Rectangle {
    x: control.leftPadding
    y: (control.height - height) / 2
    implicitWidth: control.ferryTheme.scaled(15)
    implicitHeight: control.ferryTheme.scaled(15)
    color: control.checked ? control.ferryTheme.primarySurface : control.ferryTheme.control
    border.color: control.activeFocus ? control.ferryTheme.accent : control.ferryTheme.divider
    radius: control.ferryTheme.controlRadius
    Text {
      anchors.centerIn: parent
      text: control.checked ? "✓" : ""
      color: control.checked ? control.ferryTheme.primaryText : control.ferryTheme.windowText
      font.family: control.ferryTheme.fontFamily
      font.pixelSize: control.ferryTheme.captionSize
    }
  }
  contentItem: FerryLabel {
    ferryTheme: control.ferryTheme
    leftPadding: control.indicator.width + control.spacing
    text: control.text
    color: control.enabled ? control.ferryTheme.windowText : control.ferryTheme.muted
    font.family: control.ferryTheme.fontFamily
    font.pixelSize: control.ferryTheme.bodySmallSize
    wrapMode: Text.Wrap
    verticalAlignment: Text.AlignVCenter
  }
}
