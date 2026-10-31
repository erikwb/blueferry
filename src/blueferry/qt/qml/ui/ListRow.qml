pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

// One row of every list (conversations, recent calls, notifications, tools,
// plugin items), in the style of the conversation list and Recent Calls:
// leading avatar or icon with an optional badge, a title, a dimmed subtitle,
// an optional wrapped body, a trailing meta text (time) and trailing actions
// that appear on hover or keyboard focus. Selection and hover are a tint of
// the highlight colour; keyboard focus draws the focus colour. All text is
// plain text.
Controls.ItemDelegate {
    id: row

    property string title: ""
    property string subtitle: ""
    property string body: ""
    property int bodyLines: 4
    property string meta: ""
    property string iconName: ""
    // Replaces the icon, e.g. Component { ContactAvatar { … } }.
    property Component leading: null
    property string badgeIcon: ""
    property color badgeColor: Kirigami.Theme.textColor
    property bool bold: false
    property bool unread: false
    property bool dimmed: false
    property color titleColor: Kirigami.Theme.textColor
    // "comfortable" for main lists, "compact" for the phone card.
    property string density: "comfortable"
    // Keep the trailing actions visible (e.g. a starred star).
    property bool pinActions: false
    // Explanations (phone card) wrap; list previews stay on one line.
    property bool wrapSubtitle: false
    // objectNames of the parts: <prefix>Title, Meta, Subtitle, UnreadDot, Body.
    property string partPrefix: "row"
    readonly property bool revealed: row.pinActions || row.hovered || row.visualFocus
        || row.activeFocus || actionRow.activeFocus
    default property alias actions: actionRow.data

    property int avatarSize: Kirigami.Units.iconSizes.medium

    width: ListView.view ? ListView.view.width - ListView.view.leftMargin - ListView.view.rightMargin
                         : implicitWidth
    leftPadding: Kirigami.Units.largeSpacing
    rightPadding: Kirigami.Units.smallSpacing
    topPadding: row.density === "compact" ? Kirigami.Units.smallSpacing : Kirigami.Units.largeSpacing
    bottomPadding: topPadding
    Accessible.name: [row.title, row.subtitle, row.meta].filter(part => part !== "").join(", ")

    background: Rectangle {
        radius: Kirigami.Units.cornerRadius
        color: row.highlighted || row.down
            ? Qt.alpha(Kirigami.Theme.highlightColor, 0.25)
            : row.hovered
                ? Qt.alpha(Kirigami.Theme.highlightColor, 0.1)
                : "transparent"
        border.width: row.visualFocus ? 1 : 0
        border.color: Kirigami.Theme.focusColor
    }

    contentItem: RowLayout {
        spacing: Kirigami.Units.largeSpacing
        opacity: row.dimmed || !row.enabled ? 0.6 : 1

        Item {
            Layout.alignment: Qt.AlignTop
            Layout.preferredWidth: row.avatarSize
            Layout.preferredHeight: row.avatarSize
            visible: row.leading !== null || row.iconName !== ""
            Loader {
                anchors.fill: parent
                sourceComponent: row.leading !== null ? row.leading : iconComponent
            }
            // A small status badge on the avatar (call direction).
            Rectangle {
                anchors.right: parent.right
                anchors.bottom: parent.bottom
                anchors.margins: -2
                width: Kirigami.Units.iconSizes.small
                height: width
                radius: width / 2
                visible: row.badgeIcon !== ""
                color: Kirigami.Theme.backgroundColor
                Kirigami.Icon {
                    anchors.fill: parent
                    anchors.margins: 1
                    source: row.badgeIcon
                    color: row.badgeColor
                }
            }
        }

        ColumnLayout {
            Layout.fillWidth: true
            spacing: 0
            RowLayout {
                Layout.fillWidth: true
                spacing: Kirigami.Units.smallSpacing
                Controls.Label {
                    objectName: row.partPrefix + "Title"
                    Layout.fillWidth: true
                    text: row.title
                    textFormat: Text.PlainText
                    font.bold: row.bold
                    color: row.titleColor
                    elide: Text.ElideRight
                    maximumLineCount: 1
                }
                Controls.Label {
                    objectName: row.partPrefix + "Meta"
                    visible: text !== ""
                    text: row.meta
                    textFormat: Text.PlainText
                    font: Kirigami.Theme.smallFont
                    color: row.unread ? Kirigami.Theme.highlightColor : Kirigami.Theme.disabledTextColor
                }
            }
            RowLayout {
                Layout.fillWidth: true
                visible: row.subtitle !== "" || row.unread
                spacing: Kirigami.Units.smallSpacing
                Controls.Label {
                    objectName: row.partPrefix + "Subtitle"
                    Layout.fillWidth: true
                    text: row.subtitle
                    textFormat: Text.PlainText
                    elide: Text.ElideRight
                    wrapMode: row.wrapSubtitle ? Text.Wrap : Text.NoWrap
                    maximumLineCount: row.wrapSubtitle ? 4 : 1
                    font: row.density === "compact" ? Kirigami.Theme.smallFont : Kirigami.Theme.defaultFont
                    color: row.unread ? Kirigami.Theme.textColor : Kirigami.Theme.disabledTextColor
                }
                Rectangle {
                    objectName: row.partPrefix + "UnreadDot"
                    implicitWidth: Kirigami.Units.smallSpacing * 2
                    implicitHeight: implicitWidth
                    radius: width / 2
                    color: Kirigami.Theme.highlightColor
                    visible: row.unread
                    Accessible.name: qsTr("Unread")
                }
            }
            Controls.Label {
                objectName: row.partPrefix + "Body"
                Layout.fillWidth: true
                visible: text !== ""
                text: row.body
                textFormat: Text.PlainText
                wrapMode: Text.Wrap
                maximumLineCount: row.bodyLines
                elide: Text.ElideRight
                opacity: 0.85
            }
        }

        RowLayout {
            id: actionRow
            spacing: 0
            // Drawn when pointed at, focused (keyboard) or pinned; otherwise
            // collapsed but still visible, so screen readers keep the buttons
            // and the time stays at the right edge.
            visible: children.length > 0
            opacity: row.revealed ? 1 : 0
            clip: true
            Layout.preferredWidth: row.revealed ? implicitWidth : 0
        }
    }

    Component {
        id: iconComponent
        Kirigami.Icon { source: row.iconName }
    }
}
