pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Layouts

// The recent iPhone app notifications (Keep recent iPhone notifications),
// compact and read-only. `view` is the bridge's notifications reply: the
// shared placeholder (title, explanation), the content notice and `rows`
// with app, time, summary and body.
// Every field comes from the iPhone and is shown as plain text.
ColumnLayout {
  id: root
  required property var ferryTheme
  property var view: ({})
  // A JS array from shell.qml's plainValue, or a Qt sequence; both index.
  readonly property var rows: root.view.rows && root.view.rows.length !== undefined
    ? Array.prototype.slice.call(root.view.rows) : []
  readonly property int count: root.rows.length
  spacing: root.ferryTheme.scaled(6)

  FerryLabel {
    objectName: "notificationListContentHint"
    ferryTheme: root.ferryTheme
    Layout.fillWidth: true
    visible: text !== ""
    wrapMode: Text.Wrap
    color: root.ferryTheme.muted
    font.pixelSize: root.ferryTheme.captionSize
    text: String(root.view.content_hint || "")
  }
  FerryLabel {
    objectName: "notificationListTitle"
    ferryTheme: root.ferryTheme
    Layout.fillWidth: true
    visible: root.count === 0 && text !== ""
    wrapMode: Text.Wrap
    font.bold: true
    text: String(root.view.title || "")
  }
  FerryLabel {
    objectName: "notificationListExplanation"
    ferryTheme: root.ferryTheme
    Layout.fillWidth: true
    visible: root.count === 0 && text !== ""
    wrapMode: Text.Wrap
    color: root.view.failed === true ? root.ferryTheme.warning : root.ferryTheme.windowText
    text: String(root.view.explanation || "")
  }
  Repeater {
    objectName: "notificationListRows"
    model: root.rows
    delegate: ColumnLayout {
      id: entry
      required property int index
      required property var modelData
      readonly property var row: entry.modelData || ({})
      Layout.fillWidth: true
      spacing: root.ferryTheme.scaled(1)
      FerryLabel {
        objectName: "notificationApp" + entry.index
        ferryTheme: root.ferryTheme
        Layout.fillWidth: true
        elide: Text.ElideRight
        font.bold: true
        font.pixelSize: root.ferryTheme.bodySmallSize
        text: String(entry.row.app || "") + (entry.row.time ? "  ·  " + String(entry.row.time) : "")
      }
      FerryLabel {
        objectName: "notificationSummary" + entry.index
        ferryTheme: root.ferryTheme
        Layout.fillWidth: true
        visible: text !== ""
        wrapMode: Text.Wrap
        maximumLineCount: 2
        elide: Text.ElideRight
        font.pixelSize: root.ferryTheme.bodySmallSize
        text: String(entry.row.summary || "")
      }
      FerryLabel {
        objectName: "notificationBody" + entry.index
        ferryTheme: root.ferryTheme
        Layout.fillWidth: true
        visible: text !== ""
        wrapMode: Text.Wrap
        maximumLineCount: 3
        elide: Text.ElideRight
        color: root.ferryTheme.muted
        font.pixelSize: root.ferryTheme.bodySmallSize
        text: String(entry.row.body || "")
      }
    }
  }
}
