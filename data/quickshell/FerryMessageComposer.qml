pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ScrollView {
  id: control
  required property var ferryTheme
  property alias text: editor.text
  property alias placeholderText: editor.placeholderText
  property bool flat: false
  readonly property bool multiline: editor.lineCount > 1
  signal accepted()

  Layout.minimumWidth: 0
  Layout.minimumHeight: control.ferryTheme.scaled(36)
  Layout.preferredHeight: Math.min(
    Math.max(editor.contentHeight + editor.topPadding + editor.bottomPadding + 2,
             Layout.minimumHeight),
    Layout.maximumHeight
  )
  Layout.maximumHeight: control.ferryTheme.scaled(144)
  clip: true
  ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
  ScrollBar.vertical.policy: ScrollBar.AsNeeded

  function clear() { editor.clear() }
  function forceActiveFocus() { editor.forceActiveFocus() }
  function submit(event) {
    if ((event.modifiers & Qt.ShiftModifier) !== 0) {
      event.accepted = false
      return
    }
    accepted()
    event.accepted = true
  }

  background: Rectangle {
    color: control.flat ? "transparent" : control.ferryTheme.control
    border.color: editor.activeFocus ? control.ferryTheme.accent
      : control.flat ? "transparent" : control.ferryTheme.divider
    radius: control.ferryTheme.controlRadius
  }

  TextArea {
    id: editor
    width: control.availableWidth
    leftPadding: control.ferryTheme.scaled(10)
    rightPadding: control.ferryTheme.scaled(10)
    color: control.ferryTheme.windowText
    placeholderTextColor: control.ferryTheme.muted
    selectionColor: control.ferryTheme.accent
    selectedTextColor: control.ferryTheme.highlightedText
    font.family: control.ferryTheme.fontFamily
    font.pixelSize: control.ferryTheme.baseFontSize
    selectByMouse: true
    wrapMode: TextEdit.Wrap
    verticalAlignment: control.multiline ? TextEdit.AlignTop : TextEdit.AlignVCenter
    background: null
    Accessible.name: control.Accessible.name
    Keys.onReturnPressed: event => control.submit(event)
    Keys.onEnterPressed: event => control.submit(event)
  }
}
