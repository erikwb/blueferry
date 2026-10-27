pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

Rectangle {
    id: root

    required property var message
    required property real availableWidth
    required property bool showSender
    readonly property color outgoingBackground: Qt.rgba(
        Kirigami.Theme.backgroundColor.r * 0.78
            + Kirigami.Theme.highlightColor.r * 0.22,
        Kirigami.Theme.backgroundColor.g * 0.78
            + Kirigami.Theme.highlightColor.g * 0.22,
        Kirigami.Theme.backgroundColor.b * 0.78
            + Kirigami.Theme.highlightColor.b * 0.22,
        1
    )

    readonly property bool richBody: message.body_markup !== undefined
    readonly property string bodyText: richBody
        ? "<style>a { color: " + Kirigami.Theme.linkColor + "; }</style>"
            + "<span style=\"white-space: pre-wrap;\">"
            + message.body_markup + "</span>"
        : message.body
    readonly property real maximumWidth: availableWidth * 0.75
    readonly property real naturalContentWidth: Math.max(
        naturalBody.implicitWidth + messageBody.leftPadding + messageBody.rightPadding,
        senderLabel.visible ? senderLabel.implicitWidth : 0,
        timestampLabel.implicitWidth
    )

    // A wrapping TextArea reports the width it is given, not the width its
    // text needs. This unwrapped copy measures the line length instead.
    Text {
        id: naturalBody
        visible: false
        text: root.bodyText
        textFormat: root.richBody ? Text.RichText : Text.PlainText
        font: messageBody.font
    }

    width: Math.min(root.maximumWidth, root.naturalContentWidth + Kirigami.Units.largeSpacing * 2)
    implicitHeight: bodyColumn.implicitHeight + Kirigami.Units.largeSpacing * 2
    radius: Kirigami.Units.cornerRadius * 2
    color: message.outgoing
        ? outgoingBackground
        : Kirigami.Theme.alternateBackgroundColor

    ColumnLayout {
        id: bodyColumn
        anchors.fill: parent
        anchors.margins: Kirigami.Units.largeSpacing
        spacing: Kirigami.Units.smallSpacing / 2

        Controls.Label {
            id: senderLabel
            Layout.fillWidth: true
            elide: Text.ElideRight
            text: root.message.outgoing ? qsTr("You") : (root.message.sender || "")
            visible: root.showSender
            textFormat: Text.PlainText
            font.pixelSize: Kirigami.Theme.smallFont.pixelSize
            font.weight: Font.DemiBold
            color: Kirigami.Theme.textColor
        }
        Controls.TextArea {
            id: messageBody
            objectName: "messageBody"
            Layout.fillWidth: true
            leftPadding: 0
            rightPadding: 0
            topPadding: 0
            bottomPadding: 0
            text: root.bodyText
            textFormat: root.richBody ? TextEdit.RichText : TextEdit.PlainText
            readOnly: true
            selectByMouse: true
            wrapMode: Text.Wrap
            background: null
            color: Kirigami.Theme.textColor
            Accessible.name: qsTr("Message: ") + root.message.body
            onLinkActivated: link => {
                if (/^https?:\/\//i.test(link)) Qt.openUrlExternally(link)
            }
            HoverHandler {
                cursorShape: parent.hoveredLink ? Qt.PointingHandCursor : Qt.IBeamCursor
            }
        }
        Controls.Label {
            id: timestampLabel
            Layout.alignment: root.message.outgoing ? Qt.AlignRight : Qt.AlignLeft
            text: root.message.display_timestamp || ""
            visible: text !== ""
            textFormat: Text.PlainText
            font: Kirigami.Theme.smallFont
            color: Kirigami.Theme.disabledTextColor
        }
    }
}
