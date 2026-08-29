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
  readonly property real bodyLineHeight: Math.max(
    1, Math.ceil(Math.max(bodyFontMetrics.height, bodyFontMetrics.lineSpacing)))
  readonly property real minimumBodyHeight: bubblePadding * 2 + bodyLineHeight
  readonly property bool canRenderBody: maximumHeight >= minimumBodyHeight
  readonly property bool showSenderChrome: canRenderBody && showSender
    && maximumHeight >= minimumBodyHeight
      + messageSender.implicitHeight + contentSpacing
  readonly property bool showTimestampChrome: canRenderBody
    && messageTimestamp.text !== ""
    && maximumHeight >= minimumBodyHeight
      + (showSenderChrome
         ? messageSender.implicitHeight + contentSpacing : 0)
      + messageTimestamp.implicitHeight + contentSpacing
  readonly property real naturalContentWidth: Math.max(
    showSenderChrome ? senderMetrics.advanceWidth : 0,
    Math.max(bodyMetrics.advanceWidth,
             showTimestampChrome ? timestampMetrics.advanceWidth : 0))
  readonly property real nonBodyHeight: bubblePadding * 2
    + (showSenderChrome
       ? messageSender.implicitHeight + contentSpacing : 0)
    + (showTimestampChrome
       ? messageTimestamp.implicitHeight + contentSpacing : 0)
  readonly property int maximumBodyLines: canRenderBody
    ? Math.max(1, Math.floor(
        Math.max(0, maximumHeight - nonBodyHeight) / bodyLineHeight))
    : 1
  readonly property bool bodyTruncated: canRenderBody && messageBody.truncated
  readonly property real naturalHeight: canRenderBody
    ? nonBodyHeight + messageBody.implicitHeight : 0

  width: Math.min(availableWidth * 0.76,
                  Math.max(ferryTheme.scaled(92),
                           naturalContentWidth + bubblePadding * 2))
  height: canRenderBody ? Math.min(maximumHeight, naturalHeight) : 0
  visible: canRenderBody
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
      visible: root.showSenderChrome
      text: root.message.outgoing ? "You" : (root.message.sender || "")
      textFormat: Text.PlainText
      color: root.ferryTheme.windowText
      font.family: root.ferryTheme.fontFamily
      font.pixelSize: root.ferryTheme.captionSize
      font.bold: true
    }

    TextEdit {
      id: messageBody
      width: parent.width
      text: root.message.body
      textFormat: TextEdit.PlainText
      color: root.ferryTheme.windowText
      font.family: root.ferryTheme.fontFamily
      font.pixelSize: root.ferryTheme.baseFontSize
      wrapMode: TextEdit.Wrap
      visible: root.canRenderBody
      readOnly: true
      selectByMouse: true
      activeFocusOnTab: false

      // Read-only TextEdit simulation properties for metrics compatibility
      readonly property bool truncated: false
    }

    Text {
      id: messageTimestamp
      width: parent.width
      visible: root.showTimestampChrome
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
