pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Layouts

// Running iPhone calls: caller, state or duration, Answer while it rings and
// Hang Up/Decline. Every word comes from the bridge's "calls" reply
// (phone_link_view.call_card, like the Qt, GTK and terminal clients), the
// duration included: while a call is active the shell asks the bridge for
// "call_cards" once a second, which formats the cached ListCalls without a
// D-Bus call. Caller names are shown verbatim as plain text.
ColumnLayout {
  id: root

  required property var ferryTheme
  property var view: ({})
  signal answerRequested(string callId)
  signal hangupRequested(string callId)

  // A list from the bridge or a QVariantList (tests); see shell.qml plainValue.
  readonly property var rows: root.view.calls && root.view.calls.length !== undefined
    ? root.view.calls : []

  visible: root.view.visible === true && root.rows.length > 0
  spacing: root.ferryTheme.scaled(4)

  Repeater {
    model: root.rows

    delegate: RowLayout {
      id: callRow
      required property var modelData
      objectName: "callRow"
      Layout.fillWidth: true
      spacing: root.ferryTheme.scaled(6)

      ColumnLayout {
        Layout.fillWidth: true
        spacing: 0
        FerryLabel {
          objectName: "callTitle"
          ferryTheme: root.ferryTheme
          Layout.fillWidth: true
          elide: Text.ElideRight
          textFormat: Text.PlainText
          text: "📞  " + String(callRow.modelData.title || "")
          font.bold: true
        }
        FerryLabel {
          objectName: "callSubtitle"
          ferryTheme: root.ferryTheme
          Layout.fillWidth: true
          elide: Text.ElideRight
          textFormat: Text.PlainText
          text: String(callRow.modelData.subtitle || "")
          color: root.ferryTheme.muted
          font.pixelSize: root.ferryTheme.captionSize
        }
      }
      FerryButton {
        objectName: "callAnswer"
        ferryTheme: root.ferryTheme
        visible: callRow.modelData.ringing === true
        highlighted: true
        text: String(callRow.modelData.answer || "")
        onClicked: root.answerRequested(String(callRow.modelData.call_id))
      }
      FerryButton {
        objectName: "callHangup"
        ferryTheme: root.ferryTheme
        text: String(callRow.modelData.hangup || "")
        onClicked: root.hangupRequested(String(callRow.modelData.call_id))
      }
    }
  }

  FerryLabel {
    objectName: "callAudioHint"
    ferryTheme: root.ferryTheme
    Layout.fillWidth: true
    visible: text !== ""
    wrapMode: Text.Wrap
    textFormat: Text.PlainText
    text: typeof root.view.audio_hint === "string" ? root.view.audio_hint : ""
    color: root.ferryTheme.muted
    font.pixelSize: root.ferryTheme.captionSize
  }
}
