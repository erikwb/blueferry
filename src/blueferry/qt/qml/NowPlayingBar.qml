import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

// Compact iPhone now-playing strip, shown only for opted-in media control.
Controls.ToolBar {
    id: bar
    objectName: "nowPlayingBar"

    required property var nowPlaying
    signal commandRequested(string command)

    readonly property var track: nowPlaying.track || ({})
    readonly property var player: nowPlaying.player || ({})
    readonly property var commands: nowPlaying.supported_commands || []
    readonly property bool playing: player.state === "playing"
        || player.state === "rewinding" || player.state === "fast-forwarding"

    readonly property string stateIcon: playing ? "media-playback-start" : "media-playback-pause"

    // An unknown list (the phone has not sent it yet) is not an empty one:
    // offer the transport buttons and let the phone refuse what it lacks.
    // Older backends do not report the key and always mean a known list.
    readonly property bool commandsKnown: nowPlaying.supported_commands_known !== false

    function supports(name) {
        if (!commandsKnown)
            return ["previous", "next", "toggle", "play", "pause"].indexOf(name) >= 0
        return commands.indexOf(name) >= 0
    }

    function summary() {
        const title = track.title || ""
        const artist = track.artist || ""
        if (!title && !player.state)
            return qsTr("Nothing is playing on the iPhone")
        if (title && artist)
            return qsTr("%1 — %2").arg(title).arg(artist)
        return title || artist || qsTr("Unknown title")
    }

    Accessible.name: qsTr("iPhone now playing")
    // Sits flush inside the phone card rather than as a separate tool strip.
    background: null
    leftPadding: 0
    rightPadding: 0

    contentItem: RowLayout {
        spacing: Kirigami.Units.smallSpacing

        Kirigami.Icon {
            // Shows the iPhone's current state; the toggle button shows the action.
            source: bar.stateIcon
            implicitWidth: Kirigami.Units.iconSizes.small
            implicitHeight: Kirigami.Units.iconSizes.small
        }

        Controls.Label {
            objectName: "nowPlayingSummary"
            Layout.fillWidth: true
            // Remote text is never interpreted as markup.
            textFormat: Text.PlainText
            text: bar.summary()
            elide: Text.ElideRight
        }

        Controls.ToolButton {
            objectName: "nowPlayingPrevious"
            icon.name: "media-skip-backward"
            text: qsTr("Previous")
            display: Controls.AbstractButton.IconOnly
            enabled: bar.supports("previous")
            Accessible.name: text
            Controls.ToolTip.text: text
            Controls.ToolTip.visible: hovered
            onClicked: bar.commandRequested("previous")
        }
        Controls.ToolButton {
            objectName: "nowPlayingToggle"
            icon.name: bar.playing ? "media-playback-pause" : "media-playback-start"
            text: bar.playing ? qsTr("Pause") : qsTr("Play")
            display: Controls.AbstractButton.IconOnly
            enabled: bar.supports("toggle") || bar.supports(bar.playing ? "pause" : "play")
            Accessible.name: text
            Controls.ToolTip.text: text
            Controls.ToolTip.visible: hovered
            onClicked: bar.commandRequested("toggle")
        }
        Controls.ToolButton {
            objectName: "nowPlayingNext"
            icon.name: "media-skip-forward"
            text: qsTr("Next")
            display: Controls.AbstractButton.IconOnly
            enabled: bar.supports("next")
            Accessible.name: text
            Controls.ToolTip.text: text
            Controls.ToolTip.visible: hovered
            onClicked: bar.commandRequested("next")
        }
    }
}
