pragma ComponentBehavior: Bound

import QtQuick

Item {
  id: control
  required property var ferryTheme
  property string label: ""
  property string value: ""
  implicitHeight: Math.max(infoLabel.implicitHeight, infoValue.implicitHeight)
    + control.ferryTheme.scaled(12)
  Rectangle {
    anchors.bottom: parent.bottom
    width: parent.width
    height: 1
    color: control.ferryTheme.divider
  }
  Text {
    id: infoLabel
    anchors.left: parent.left
    anchors.leftMargin: control.ferryTheme.scaled(12)
    anchors.verticalCenter: parent.verticalCenter
    text: parent.label
    color: control.ferryTheme.muted
    font.family: control.ferryTheme.fontFamily
    font.pixelSize: control.ferryTheme.bodySmallSize
  }
  Text {
    id: infoValue
    anchors.right: parent.right
    anchors.rightMargin: control.ferryTheme.scaled(12)
    anchors.verticalCenter: parent.verticalCenter
    text: parent.value
    color: control.ferryTheme.windowText
    font.family: control.ferryTheme.fontFamily
    font.pixelSize: control.ferryTheme.bodySmallSize
  }
}
