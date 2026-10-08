pragma ComponentBehavior: Bound

import QtQuick

Rectangle {
  id: avatar
  required property var ferryTheme
  required property var photos
  property var thread: null
  property bool highlighted: false
  property int avatarSize: ferryTheme.scaled(26)
  readonly property int decodeSize: Math.min(512, Math.ceil(avatarSize * Screen.devicePixelRatio))
  readonly property string address: thread && !thread.is_group
    && thread.recipients && thread.recipients.length === 1 ? thread.recipients[0] : ""
  readonly property string photoSource: photos.revision >= 0 ? photos.source(address) : ""
  readonly property bool photoReady: photo.status === Image.Ready && photo.source.toString() !== ""
  implicitWidth: avatarSize
  implicitHeight: avatarSize
  radius: ferryTheme.controlRadius
  color: ferryTheme.control
  border.color: ferryTheme.divider
  clip: true

  Text {
    anchors.centerIn: parent
    visible: !avatar.photoReady
    text: avatar.thread && avatar.thread.is_group ? "#"
      : String(avatar.thread ? avatar.thread.name || "?" : "?").charAt(0).toUpperCase()
    textFormat: Text.PlainText
    color: avatar.highlighted ? avatar.ferryTheme.accent : avatar.ferryTheme.muted
    font.family: avatar.ferryTheme.fontFamily
    font.pixelSize: avatar.ferryTheme.baseFontSize
    font.bold: true
  }
  Image {
    id: photo
    anchors.fill: parent
    source: avatar.photoSource
    sourceSize: Qt.size(avatar.decodeSize, avatar.decodeSize)
    fillMode: Image.PreserveAspectCrop
    asynchronous: true
    cache: false
    visible: avatar.photoReady
  }
}
