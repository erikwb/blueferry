pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls

Button {
  id: control
  required property var ferryTheme
  property real labelSize: control.ferryTheme.baseFontSize
  property bool bare: false
  property bool subtle: false
  property bool labelBold: false
  property real labelLetterSpacing: 0
  implicitHeight: control.ferryTheme.scaled(28)
  leftPadding: control.ferryTheme.scaled(12)
  rightPadding: control.ferryTheme.scaled(12)
  topPadding: control.ferryTheme.scaled(5)
  bottomPadding: control.ferryTheme.scaled(5)

  contentItem: Text {
    text: control.text
    textFormat: Text.PlainText
    color: !control.enabled ? control.ferryTheme.muted
      : control.bare && control.hovered ? control.ferryTheme.accent
      : control.highlighted ? control.ferryTheme.primaryText
      : control.subtle ? control.ferryTheme.muted : control.ferryTheme.windowText
    font.family: control.ferryTheme.fontFamily
    font.pixelSize: control.labelSize
    font.bold: control.labelBold || control.highlighted
    font.letterSpacing: control.labelLetterSpacing
    horizontalAlignment: Text.AlignHCenter
    verticalAlignment: Text.AlignVCenter
    elide: Text.ElideRight
  }
  background: Rectangle {
    color: control.bare ? "transparent"
      : control.highlighted
      ? control.ferryTheme.primarySurface
      : control.down || control.checked ? control.ferryTheme.selectedSurface
        : control.hovered ? control.ferryTheme.hoverSurface : control.ferryTheme.control
    border.color: control.bare
      ? control.activeFocus ? control.ferryTheme.accent : "transparent"
      : control.activeFocus ? control.ferryTheme.accent
      : control.highlighted ? control.ferryTheme.accent : control.ferryTheme.divider
    radius: control.ferryTheme.controlRadius
    opacity: control.enabled ? 1.0 : 0.55
  }
}
