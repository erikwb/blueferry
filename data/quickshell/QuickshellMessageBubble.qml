import QtQuick

Rectangle {
  id: root

  required property var message
  required property real availableWidth
  required property real availableHeight
  required property bool showSender
  required property var ferryTheme

  readonly property int bubblePadding: ferryTheme.scaled(12)
  readonly property int contentSpacing: ferryTheme.scaled(5)
  readonly property real maximumHeight: Math.max(
    0, availableHeight - ferryTheme.scaled(3))
  readonly property real naturalContentWidth: Math.max(
    messageSender.visible ? senderMetrics.advanceWidth : 0,
    Math.max(bodyMetrics.advanceWidth,
             messageTimestamp.visible ? timestampMetrics.advanceWidth : 0))
  readonly property real nonBodyHeight: bubblePadding * 2
    + (messageSender.visible
       ? messageSender.implicitHeight + contentSpacing : 0)
    + (messageTimestamp.visible
       ? messageTimestamp.implicitHeight + contentSpacing : 0)
  readonly property int maximumBodyLines: Math.max(
    1, Math.floor((maximumHeight - nonBodyHeight) / bodyFontMetrics.lineSpacing))
  readonly property bool bodyTruncated: messageBody.truncated

  width: Math.min(availableWidth * 0.76,
                  Math.max(ferryTheme.scaled(92),
                           naturalContentWidth + bubblePadding * 2))
  height: Math.min(maximumHeight,
                   bubbleContent.implicitHeight + bubblePadding * 2)
  clip: true
  color: message.outgoing
    ? ferryTheme.selectedSurface : ferryTheme.raisedSurface
  border.color: message.outgoing ? "transparent" : ferryTheme.divider
  radius: ferryTheme.controlRadius

  TextMetrics {
    id: senderMetrics
    font: messageSender.font
    text: messageSender.text
  }

  TextMetrics {
    id: bodyMetrics
    font: messageBody.font
    text: messageBody.text
  }

  TextMetrics {
    id: timestampMetrics
    font: messageTimestamp.font
    text: messageTimestamp.text
  }

  FontMetrics {
    id: bodyFontMetrics
    font: messageBody.font
  }

  Column {
    id: bubbleContent
    anchors.left: parent.left
    anchors.right: parent.right
    anchors.top: parent.top
    anchors.margins: root.bubblePadding
    spacing: root.contentSpacing

    Text {
      id: messageSender
      width: parent.width
      visible: root.showSender
      text: root.message.outgoing ? "You" : (root.message.sender || "")
      textFormat: Text.PlainText
      color: root.ferryTheme.windowText
      font.family: root.ferryTheme.fontFamily
      font.pixelSize: root.ferryTheme.captionSize
      font.bold: true
    }

    Text {
      id: messageBody
      width: parent.width
      text: root.message.body
      textFormat: Text.PlainText
      color: root.ferryTheme.windowText
      font.family: root.ferryTheme.fontFamily
      font.pixelSize: root.ferryTheme.baseFontSize
      wrapMode: Text.Wrap
      maximumLineCount: root.maximumBodyLines
      elide: Text.ElideRight
    }

    Text {
      id: messageTimestamp
      width: parent.width
      visible: text !== ""
      text: root.message.display_timestamp || ""
      textFormat: Text.PlainText
      color: root.message.outgoing
        ? Qt.rgba(root.ferryTheme.windowText.r,
                  root.ferryTheme.windowText.g,
                  root.ferryTheme.windowText.b, 0.62)
        : root.ferryTheme.muted
      font.family: root.ferryTheme.fontFamily
      font.pixelSize: root.ferryTheme.captionSize
      horizontalAlignment: Text.AlignRight
    }
  }
}
