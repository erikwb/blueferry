pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Layouts

// A settings category (Phone, Notifications, Calls & Audio, Security,
// About), the same categories and order as the Qt, GTK and terminal
// clients. The section labels below it name the groups inside.
ColumnLayout {
  id: control
  required property var ferryTheme
  property string text: ""
  spacing: control.ferryTheme.scaled(4)
  Layout.fillWidth: true
  Layout.topMargin: control.ferryTheme.scaled(14)

  FerryLabel {
    ferryTheme: control.ferryTheme
    text: control.text
    font.pixelSize: control.ferryTheme.headingSize
    font.bold: true
    Accessible.role: Accessible.Heading
  }
  Rectangle {
    Layout.fillWidth: true
    implicitHeight: 1
    color: control.ferryTheme.divider
  }
}
