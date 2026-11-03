pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// The recent iPhone app notifications (Keep recent iPhone notifications),
// compact. `view` is the bridge's notifications reply: the shared
// placeholder (title, explanation), the content notice, the pin limit text
// and `rows` with app, time, summary and body plus the shared pin fields
// (notification_pin_view: key, can_pin, pinned, pin_label, pin_tooltip,
// pinned_text, removed_text) and `dimmed` for removed and muted apps.
// Pinning is the only action here; the panel has no other row actions.
// Every field comes from the iPhone or the bridge and is shown as plain text.
ColumnLayout {
  id: root
  required property var ferryTheme
  property var view: ({})
  // A JS array from shell.qml's plainValue, or a Qt sequence; both index.
  readonly property var rows: root.view.rows && root.view.rows.length !== undefined
    ? Array.prototype.slice.call(root.view.rows) : []
  readonly property int count: root.rows.length
  readonly property bool pinLimitReached: root.view.pin_limit_reached === true
  // A pin request is on its way; the bridge's refusal in shared wording.
  property bool pinBusy: false
  property string pinError: ""
  signal pinRequested(string key, bool pinned)
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
  FerryLabel {
    objectName: "notificationPinLimit"
    ferryTheme: root.ferryTheme
    Layout.fillWidth: true
    visible: root.count > 0 && text !== ""
    wrapMode: Text.Wrap
    color: root.ferryTheme.muted
    font.pixelSize: root.ferryTheme.captionSize
    text: String(root.view.pin_limit_text || "")
  }
  FerryLabel {
    objectName: "notificationPinError"
    ferryTheme: root.ferryTheme
    Layout.fillWidth: true
    visible: text !== ""
    wrapMode: Text.Wrap
    color: root.ferryTheme.warning
    text: root.pinError
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
      // Removed on the iPhone or from a muted app: greyed, still listed.
      opacity: entry.row.dimmed === true ? 0.55 : 1.0
      RowLayout {
        Layout.fillWidth: true
        spacing: root.ferryTheme.scaled(6)
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
          objectName: "notificationPinned" + entry.index
          ferryTheme: root.ferryTheme
          visible: text !== ""
          color: root.ferryTheme.accent
          font.pixelSize: root.ferryTheme.captionSize
          text: String(entry.row.pinned_text || "")
        }
        FerryButton {
          objectName: "notificationPin" + entry.index
          ferryTheme: root.ferryTheme
          visible: entry.row.can_pin === true
          text: String(entry.row.pin_label || "")
          labelSize: root.ferryTheme.captionSize
          implicitHeight: root.ferryTheme.scaled(22)
          // Unpinning always works; a further pin waits for a free place.
          enabled: !root.pinBusy && (entry.row.pinned === true || !root.pinLimitReached)
          Accessible.name: text + ": " + String(entry.row.app || "")
          Accessible.description: entry.row.pinned !== true && root.pinLimitReached
            ? String(root.view.pin_limit_text || "") : String(entry.row.pin_tooltip || "")
          ToolTip.visible: hovered && ToolTip.text !== ""
          ToolTip.text: String(entry.row.pin_tooltip || "")
          onClicked: root.pinRequested(String(entry.row.key || ""), entry.row.pinned !== true)
        }
      }
      FerryLabel {
        objectName: "notificationRemoved" + entry.index
        ferryTheme: root.ferryTheme
        Layout.fillWidth: true
        visible: text !== ""
        color: root.ferryTheme.muted
        font.italic: true
        font.pixelSize: root.ferryTheme.captionSize
        text: String(entry.row.removed_text || "")
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
