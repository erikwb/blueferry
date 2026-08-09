pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

Rectangle {
    id: root

    required property var message
    required property real availableWidth

    width: Math.min(
        availableWidth * 0.78,
        bodyColumn.implicitWidth + Kirigami.Units.largeSpacing * 2
    )
    implicitHeight: bodyColumn.implicitHeight + Kirigami.Units.largeSpacing
    radius: Kirigami.Units.cornerRadius
    color: message.outgoing
        ? Kirigami.Theme.highlightColor
        : Kirigami.Theme.alternateBackgroundColor

    ColumnLayout {
        id: bodyColumn
        anchors.fill: parent
        anchors.margins: Kirigami.Units.smallSpacing
        width: Math.min(implicitWidth, root.availableWidth * 0.72)

        Controls.TextArea {
            Layout.maximumWidth: root.availableWidth * 0.7
            text: root.message.body
            textFormat: TextEdit.PlainText
            readOnly: true
            selectByMouse: true
            wrapMode: Text.Wrap
            background: null
            color: root.message.outgoing
                ? Kirigami.Theme.highlightedTextColor
                : Kirigami.Theme.textColor
            Accessible.name: qsTr("Message: ") + text
        }
        Controls.Label {
            text: root.message.display_timestamp || ""
            visible: text !== ""
            textFormat: Text.PlainText
            font: Kirigami.Theme.smallFont
            opacity: 0.7
            color: root.message.outgoing
                ? Kirigami.Theme.highlightedTextColor
                : Kirigami.Theme.textColor
        }
    }
}
