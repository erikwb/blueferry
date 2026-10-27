pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

// One plugin from a plugin index. The index is untrusted: every text is
// plain text, the icon is a theme icon name, and "Install" only starts the
// confirmed install flow.
Kirigami.AbstractCard {
    id: card
    required property var modelData
    property bool busy: false
    signal installRequested(string id)
    objectName: "storeCard_" + card.modelData.id
    opacity: card.modelData.state === "soon" ? 0.6 : 1

    contentItem: ColumnLayout {
        spacing: Kirigami.Units.smallSpacing
        RowLayout {
            Layout.fillWidth: true
            spacing: Kirigami.Units.largeSpacing
            Kirigami.Icon {
                visible: (card.modelData.emoji || "") === ""
                source: card.modelData.icon
                Layout.preferredWidth: Kirigami.Units.iconSizes.medium
                Layout.preferredHeight: Kirigami.Units.iconSizes.medium
            }
            Controls.Label {
                visible: (card.modelData.emoji || "") !== ""
                text: card.modelData.emoji || ""
                textFormat: Text.PlainText
                font.pixelSize: Kirigami.Units.iconSizes.medium * 0.8
            }
            Kirigami.Heading {
                Layout.fillWidth: true
                level: 3
                text: card.modelData.name
                textFormat: Text.PlainText
                elide: Text.ElideRight
            }
        }
        Controls.Label {
            Layout.fillWidth: true
            wrapMode: Text.Wrap
            textFormat: Text.PlainText
            text: card.modelData.description || ""
        }
        Flow {
            Layout.fillWidth: true
            spacing: Kirigami.Units.smallSpacing
            Repeater {
                model: card.modelData.badges || []
                delegate: Controls.Label {
                    required property string modelData
                    text: modelData
                    textFormat: Text.PlainText
                    font: Kirigami.Theme.smallFont
                    leftPadding: Kirigami.Units.smallSpacing
                    rightPadding: Kirigami.Units.smallSpacing
                    background: Rectangle {
                        radius: Kirigami.Units.cornerRadius
                        color: Kirigami.Theme.alternateBackgroundColor
                        border.color: Kirigami.Theme.disabledTextColor
                        border.width: 1
                    }
                }
            }
        }
        RowLayout {
            Layout.fillWidth: true
            Controls.Label {
                Layout.fillWidth: true
                visible: card.modelData.state === "update"
                text: qsTr("%1 → %2").arg(card.modelData.installedRef).arg(card.modelData.ref)
                textFormat: Text.PlainText
                font: Kirigami.Theme.smallFont
            }
            Item { Layout.fillWidth: card.modelData.state !== "update" }
            Controls.Button {
                objectName: "storeAction"
                text: card.modelData.stateText
                // "Installed ✓" carries its own check mark.
                icon.name: card.modelData.state === "update" ? "update-none"
                    : card.modelData.state === "installed" ? "" : "list-add"
                enabled: card.modelData.installable === true && !card.busy
                onClicked: card.installRequested(card.modelData.id)
            }
        }
    }
}
