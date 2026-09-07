pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls

ComboBox {
  id: control
  required property var ferryTheme
  implicitHeight: control.ferryTheme.scaled(28)
  leftPadding: control.ferryTheme.scaled(10)
  rightPadding: control.ferryTheme.scaled(30)
  font.family: control.ferryTheme.fontFamily
  font.pixelSize: control.ferryTheme.baseFontSize

  contentItem: FerryLabel {
    ferryTheme: control.ferryTheme
    text: control.displayText
    color: control.enabled ? control.ferryTheme.windowText : control.ferryTheme.muted
    font: control.font
    verticalAlignment: Text.AlignVCenter
    elide: Text.ElideRight
  }
  indicator: Text {
    x: control.width - width - control.ferryTheme.scaled(10)
    y: (control.height - height) / 2
    text: "⌄"
    color: control.ferryTheme.muted
    font.family: control.ferryTheme.fontFamily
    font.pixelSize: control.ferryTheme.baseFontSize
  }
  background: Rectangle {
    color: control.hovered ? control.ferryTheme.hoverSurface : control.ferryTheme.control
    border.color: control.activeFocus ? control.ferryTheme.accent : control.ferryTheme.divider
    radius: control.ferryTheme.controlRadius
  }
  delegate: ItemDelegate {
    id: option
    required property var modelData
    required property int index
    width: control.width
    highlighted: control.highlightedIndex === index
    contentItem: Text {
      text: control.textRole ? String(option.modelData[control.textRole] || "")
        : String(option.modelData)
      textFormat: Text.PlainText
      color: control.ferryTheme.windowText
      font: control.font
      verticalAlignment: Text.AlignVCenter
      elide: Text.ElideRight
    }
    background: Rectangle {
      color: option.highlighted ? control.ferryTheme.selectedSurface
        : option.hovered ? control.ferryTheme.hoverSurface : control.ferryTheme.windowSurface
    }
  }
  popup: Popup {
    y: control.height + 1
    width: control.width
    implicitHeight: Math.min(contentItem.implicitHeight, control.ferryTheme.scaled(240))
    padding: 1
    contentItem: ListView {
      clip: true
      implicitHeight: contentHeight
      model: control.popup.visible ? control.delegateModel : null
      currentIndex: control.highlightedIndex
      ScrollIndicator.vertical: ScrollIndicator { }
    }
    background: Rectangle {
      color: control.ferryTheme.windowSurface
      border.color: control.ferryTheme.divider
      radius: control.ferryTheme.controlRadius
    }
  }
}
