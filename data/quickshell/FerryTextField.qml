pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls

TextField {
  id: control
  required property var ferryTheme
  property bool flat: false
  implicitHeight: control.ferryTheme.scaled(28)
  leftPadding: control.ferryTheme.scaled(10)
  rightPadding: control.ferryTheme.scaled(10)
  color: control.ferryTheme.windowText
  placeholderTextColor: control.ferryTheme.muted
  selectionColor: control.ferryTheme.accent
  selectedTextColor: control.ferryTheme.highlightedText
  font.family: control.ferryTheme.fontFamily
  font.pixelSize: control.ferryTheme.baseFontSize
  selectByMouse: true
  background: Rectangle {
    color: control.flat ? "transparent" : control.ferryTheme.control
    border.color: control.activeFocus ? control.ferryTheme.accent
      : control.flat ? "transparent" : control.ferryTheme.divider
    radius: control.ferryTheme.controlRadius
  }
}
