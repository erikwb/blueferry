pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

// A card like the running-call card: leading avatar or icon, a level 3
// title with a dimmed subtitle, free content below and a row of buttons.
// Children go into the content column; buttons into `actions`.
Kirigami.AbstractCard {
    id: frame
    property string title: ""
    property string subtitle: ""
    property string iconName: ""
    // Replaces the icon, e.g. Component { ContactAvatar { … } }.
    property Component leading: null
    property alias subtitleItem: subtitleLabel
    default property alias content: body.data
    property alias actions: actionRow.data

    contentItem: ColumnLayout {
        spacing: Kirigami.Units.smallSpacing
        RowLayout {
            Layout.fillWidth: true
            spacing: Kirigami.Units.largeSpacing
            visible: frame.title !== ""
            Loader {
                Layout.preferredWidth: Kirigami.Units.iconSizes.large
                Layout.preferredHeight: Kirigami.Units.iconSizes.large
                visible: frame.leading !== null || frame.iconName !== ""
                sourceComponent: frame.leading !== null ? frame.leading : iconComponent
            }
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 0
                Kirigami.Heading {
                    Layout.fillWidth: true
                    level: 3
                    text: frame.title
                    textFormat: Text.PlainText
                    elide: Text.ElideRight
                }
                Controls.Label {
                    id: subtitleLabel
                    Layout.fillWidth: true
                    visible: text !== ""
                    text: frame.subtitle
                    textFormat: Text.PlainText
                    wrapMode: Text.Wrap
                    opacity: 0.7
                }
            }
        }
        ColumnLayout {
            id: body
            Layout.fillWidth: true
            spacing: Kirigami.Units.smallSpacing
        }
        Flow {
            id: actionRow
            Layout.fillWidth: true
            spacing: Kirigami.Units.smallSpacing
            visible: children.length > 0
        }
    }

    Component {
        id: iconComponent
        Kirigami.Icon { source: frame.iconName }
    }
}
