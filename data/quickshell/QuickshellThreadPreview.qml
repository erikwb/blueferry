import QtQuick

Text {
  id: root

  required property var thread
  required property var ferryTheme

  readonly property var latestMessage: thread.messages.length
    ? thread.messages[thread.messages.length - 1] : null

  text: latestMessage
    ? (latestMessage.outgoing ? "You: " : "") + latestMessage.body
    : "No messages"
  textFormat: Text.PlainText
  color: ferryTheme.muted
  font.family: ferryTheme.fontFamily
  font.pixelSize: ferryTheme.captionSize
  wrapMode: Text.NoWrap
  maximumLineCount: 1
  elide: Text.ElideRight
  clip: true
}
