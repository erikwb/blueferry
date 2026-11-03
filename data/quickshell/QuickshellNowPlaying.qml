pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Opt-in iPhone now playing with previous, play/pause and next, matching the
// Qt now-playing bar. The bridge's now_playing call reports each button as
// its own boolean, so no array crosses into QML, and passes track text
// verbatim; FerryLabel shows it as plain text. English like the rest of this
// shell, which loads no translation catalog (po/README.md). The volume
// buttons, level and tooltips are the bridge's `volume` object, the shared
// media_volume_view wording; one press is one iPhone volume step.
RowLayout {
  id: root

  required property var ferryTheme
  property var view: ({})
  signal commandRequested(string command)

  readonly property string summary: root.summaryText()
  readonly property var volume: root.view.volume || ({})
  readonly property bool volumeShown: root.volume.visible === true

  function summaryText(): string {
    if (root.view.idle === true) return "Nothing is playing on the iPhone"
    const title = typeof root.view.title === "string" ? root.view.title : ""
    const artist = typeof root.view.artist === "string" ? root.view.artist : ""
    if (title && artist) return title + " — " + artist
    return title || artist || "Unknown title"
  }

  visible: root.view.visible === true
  spacing: root.ferryTheme.scaled(6)

  FerryLabel {
    objectName: "nowPlayingSummary"
    ferryTheme: root.ferryTheme
    Layout.fillWidth: true
    elide: Text.ElideRight
    text: (root.view.playing === true ? "▶  " : "⏸  ") + root.summary
    font.pixelSize: root.ferryTheme.captionSize
  }
  FerryButton {
    objectName: "nowPlayingPrevious"
    ferryTheme: root.ferryTheme
    text: "⏮"
    enabled: root.view.previous === true
    Accessible.name: "Previous track"
    onClicked: root.commandRequested("previous")
  }
  FerryButton {
    objectName: "nowPlayingToggle"
    ferryTheme: root.ferryTheme
    text: root.view.playing === true ? "⏸" : "▶"
    enabled: root.view.toggle === true
    Accessible.name: root.view.playing === true ? "Pause" : "Play"
    onClicked: root.commandRequested("toggle")
  }
  FerryButton {
    objectName: "nowPlayingNext"
    ferryTheme: root.ferryTheme
    text: "⏭"
    enabled: root.view.next === true
    Accessible.name: "Next track"
    onClicked: root.commandRequested("next")
  }
  FerryLabel {
    objectName: "nowPlayingVolumeLevel"
    ferryTheme: root.ferryTheme
    visible: root.volumeShown && text !== ""
    color: root.ferryTheme.muted
    font.pixelSize: root.ferryTheme.captionSize
    text: String(root.volume.level_text || "")
  }
  FerryButton {
    objectName: "nowPlayingVolumeDown"
    ferryTheme: root.ferryTheme
    visible: root.volumeShown
    text: "🔉"
    enabled: root.volume.down_enabled === true
    Accessible.name: String(root.volume.down_label || "")
    Accessible.description: String(root.volume.down_tooltip || "")
    ToolTip.visible: hovered && ToolTip.text !== ""
    ToolTip.text: String(root.volume.down_tooltip || "")
    onClicked: root.commandRequested(String(root.volume.down_command || "volume-down"))
  }
  FerryButton {
    objectName: "nowPlayingVolumeUp"
    ferryTheme: root.ferryTheme
    visible: root.volumeShown
    text: "🔊"
    enabled: root.volume.up_enabled === true
    Accessible.name: String(root.volume.up_label || "")
    Accessible.description: String(root.volume.up_tooltip || "")
    ToolTip.visible: hovered && ToolTip.text !== ""
    ToolTip.text: String(root.volume.up_tooltip || "")
    onClicked: root.commandRequested(String(root.volume.up_command || "volume-up"))
  }
}
