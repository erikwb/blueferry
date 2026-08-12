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

    width: Math.min(
        availableWidth * 0.78,
        bodyColumn.implicitWidth + Kirigami.Units.largeSpacing * 2
    )
    implicitHeight: bodyColumn.implicitHeight + Kirigami.Units.largeSpacing
    radius: Kirigami.Units.cornerRadius
    color: message.outgoing
        ? outgoingBackground
        : Kirigami.Theme.alternateBackgroundColor

    ColumnLayout {
        id: bodyColumn
        anchors.fill: parent
        anchors.margins: Kirigami.Units.smallSpacing
        width: Math.min(implicitWidth, root.availableWidth * 0.72)

        Controls.Label {
            Layout.maximumWidth: root.availableWidth * 0.7
            text: root.message.outgoing ? qsTr("You") : (root.message.sender || "")
            visible: root.showSender
            textFormat: Text.PlainText
            font.pixelSize: Kirigami.Theme.smallFont.pixelSize
            font.weight: Font.DemiBold
            color: Kirigami.Theme.textColor
        }
        Controls.TextArea {
            Layout.maximumWidth: root.availableWidth * 0.7
            text: root.message.body
            textFormat: TextEdit.PlainText
            readOnly: true
            selectByMouse: true
            wrapMode: Text.Wrap
            background: null
            color: Kirigami.Theme.textColor
            Accessible.name: qsTr("Message: ") + text
        }
        Controls.Label {
            text: root.message.display_timestamp || ""
            visible: text !== ""
            textFormat: Text.PlainText
            font: Kirigami.Theme.smallFont
            opacity: 0.7
            color: Kirigami.Theme.textColor
        }
    }
}
