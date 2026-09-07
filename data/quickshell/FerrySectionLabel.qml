pragma ComponentBehavior: Bound

import QtQuick

FerryLabel {
  id: control
  color: control.ferryTheme.muted
  font.family: control.ferryTheme.fontFamily
  font.pixelSize: control.ferryTheme.captionSize
  font.bold: true
  font.capitalization: Font.AllUppercase
  font.letterSpacing: 1
  topPadding: control.ferryTheme.scaled(12)
  bottomPadding: control.ferryTheme.scaled(2)
}
