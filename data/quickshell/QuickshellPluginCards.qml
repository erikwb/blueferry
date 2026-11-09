pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Layouts

// "From Plugins": what the enabled card plugins offer (a calendar entry,
// AirPods, a LocalSend device), with their simple buttons, like the phone
// card of the Qt and GTK clients. `view` is the bridge's plugin_cards
// reply: plain titles and labels, and no button that sends files (the
// panel has no file picker). A plugin that failed shows its dimmed hint.
// English like the rest of this shell; the hints come from the shared code.
ColumnLayout {
  id: root

  required property var ferryTheme
  property var view: ({})
  property bool busy: false
  signal actionRequested(string pluginId, string itemId, string actionId)
  signal refreshRequested()

  readonly property var cards: root.listOf(root.view.cards)
  readonly property string message: typeof root.view.message === "string" ? root.view.message : ""

  // Lists may arrive as sequence wrappers that fail Array.isArray.
  function listOf(value: var): var {
    return value !== null && typeof value === "object" && typeof value.length === "number"
      ? Array.prototype.slice.call(value) : []
  }

  visible: root.cards.length > 0 || root.message !== ""
  spacing: root.ferryTheme.scaled(4)

  RowLayout {
    Layout.fillWidth: true
    FerrySectionLabel {
      ferryTheme: root.ferryTheme
      text: "From Plugins"
      Layout.fillWidth: true
    }
    FerryButton {
      objectName: "pluginCardsRefresh"
      ferryTheme: root.ferryTheme
      bare: true
      text: "↻"
      enabled: !root.busy
      Accessible.name: "Refresh"
      onClicked: root.refreshRequested()
    }
  }

  Repeater {
    model: root.cards

    ColumnLayout {
      id: card
      required property var modelData
      Layout.fillWidth: true
      spacing: root.ferryTheme.scaled(2)

      FerryLabel {
        ferryTheme: root.ferryTheme
        visible: root.cards.length > 1 || card.modelData.ok !== true
        text: String(card.modelData.name || "")
        font.bold: true
        font.pixelSize: root.ferryTheme.captionSize
        Layout.fillWidth: true
        elide: Text.ElideRight
      }
      FerryLabel {
        objectName: "pluginCardHint"
        ferryTheme: root.ferryTheme
        visible: card.modelData.ok !== true
        text: String(card.modelData.hint || "")
        color: root.ferryTheme.muted
        font.pixelSize: root.ferryTheme.captionSize
        wrapMode: Text.Wrap
        Layout.fillWidth: true
      }

      Repeater {
        model: root.listOf(card.modelData.items)

        RowLayout {
          id: item
          required property var modelData
          Layout.fillWidth: true
          spacing: root.ferryTheme.scaled(6)

          ColumnLayout {
            Layout.fillWidth: true
            spacing: 0
            FerryLabel {
              objectName: "pluginCardTitle"
              ferryTheme: root.ferryTheme
              text: String(item.modelData.title || "")
              Layout.fillWidth: true
              elide: Text.ElideRight
            }
            FerryLabel {
              ferryTheme: root.ferryTheme
              visible: text !== ""
              text: String(item.modelData.subtitle || "")
              color: root.ferryTheme.muted
              font.pixelSize: root.ferryTheme.captionSize
              Layout.fillWidth: true
              elide: Text.ElideRight
            }
          }

          Repeater {
            model: root.listOf(item.modelData.actions)

            FerryButton {
              id: actionButton
              required property var modelData
              objectName: "pluginCardAction"
              ferryTheme: root.ferryTheme
              text: String(actionButton.modelData.label || "")
              highlighted: actionButton.modelData.primary === true
              enabled: !root.busy
              onClicked: root.actionRequested(
                String(card.modelData.pluginId || ""), String(item.modelData.id || ""),
                String(actionButton.modelData.id || ""))
            }
          }
        }
      }
    }
  }

  FerryLabel {
    objectName: "pluginCardsMessage"
    ferryTheme: root.ferryTheme
    visible: root.message !== ""
    text: root.message
    color: root.view.messageOk === false ? root.ferryTheme.urgent : root.ferryTheme.muted
    font.pixelSize: root.ferryTheme.captionSize
    wrapMode: Text.Wrap
    Layout.fillWidth: true
  }
  FerryLabel {
    objectName: "pluginCardsSendHint"
    ferryTheme: root.ferryTheme
    visible: text !== ""
    text: typeof root.view.sendHint === "string" ? root.view.sendHint : ""
    color: root.ferryTheme.muted
    font.pixelSize: root.ferryTheme.captionSize
    wrapMode: Text.Wrap
    Layout.fillWidth: true
  }
}
