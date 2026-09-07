pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls

Label {
  id: control
  required property var ferryTheme
  color: control.ferryTheme.windowText
  textFormat: Text.PlainText
  font.family: control.ferryTheme.fontFamily
  font.pixelSize: control.ferryTheme.baseFontSize
}
