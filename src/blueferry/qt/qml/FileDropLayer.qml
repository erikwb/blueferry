pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

// The main window's drop zone (file_drop_view.py): the "Drop to send to
// iPhone" overlay while files hover over the window, the picker when
// several "Send to…" targets exist and the explanation when none does.
// The DropArea itself lives in Main.qml below the pages, so files dropped
// on a plugin card item still go to that item. Every text comes from the
// bridge as plain text.
Item {
    id: dropZone
    objectName: "fileDropLayer"

    required property var bridge
    // Files hover over the window.
    property bool active: false
    signal pluginSettingsRequested()

    readonly property var dropState: dropZone.bridge.fileDrop || ({})
    readonly property string phase: dropZone.dropState.phase || "idle"

    onPhaseChanged: {
        if (dropZone.phase === "picking") {
            pickerDialog.selectedKey = dropZone.dropState.selected || ""
            pickerDialog.open()
        } else {
            pickerDialog.close()
        }
        if (dropZone.phase === "none")
            noTargetDialog.open()
        else
            noTargetDialog.close()
    }

    Rectangle {
        objectName: "fileDropOverlay"
        anchors.fill: parent
        visible: dropZone.active
        color: Qt.rgba(Kirigami.Theme.highlightColor.r, Kirigami.Theme.highlightColor.g,
                       Kirigami.Theme.highlightColor.b, 0.18)
        border.color: Kirigami.Theme.highlightColor
        border.width: 2
        radius: Kirigami.Units.cornerRadius

        ColumnLayout {
            anchors.centerIn: parent
            width: Math.min(parent.width - Kirigami.Units.gridUnit * 2, Kirigami.Units.gridUnit * 24)
            spacing: Kirigami.Units.smallSpacing

            Kirigami.Icon {
                source: "document-send"
                Layout.alignment: Qt.AlignHCenter
                Layout.preferredWidth: Kirigami.Units.iconSizes.huge
                Layout.preferredHeight: Kirigami.Units.iconSizes.huge
            }
            Kirigami.Heading {
                objectName: "fileDropOverlayTitle"
                text: dropZone.dropState.overlayTitle || ""
                textFormat: Text.PlainText
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.Wrap
                Layout.fillWidth: true
            }
            Controls.Label {
                text: dropZone.dropState.overlaySubtitle || ""
                textFormat: Text.PlainText
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.Wrap
                Layout.fillWidth: true
            }
        }
    }

    Kirigami.Dialog {
        id: pickerDialog
        objectName: "fileDropPicker"
        property string selectedKey: ""
        title: dropZone.dropState.title || ""
        standardButtons: Kirigami.Dialog.Cancel
        preferredWidth: Kirigami.Units.gridUnit * 22
        // Closed without picking (Cancel, Escape, a click outside): no send.
        onClosed: if (dropZone.phase === "picking") dropZone.bridge.cancelDrop()
        customFooterActions: [
            Kirigami.Action {
                text: dropZone.dropState.sendLabel || ""
                icon.name: "document-send"
                enabled: pickerDialog.selectedKey !== ""
                // The selection stays: a pick refused while "Send to…" is
                // busy keeps the picker open with the same target.
                onTriggered: dropZone.bridge.pickDropTarget(pickerDialog.selectedKey)
            }
        ]

        ColumnLayout {
            spacing: Kirigami.Units.smallSpacing

            Controls.Label {
                text: dropZone.dropState.body || ""
                textFormat: Text.PlainText
                wrapMode: Text.Wrap
                Layout.fillWidth: true
            }
            Repeater {
                model: dropZone.dropState.choices || []
                delegate: Controls.RadioButton {
                    id: choiceButton
                    required property var modelData
                    objectName: "fileDropChoice_" + choiceButton.modelData.key
                    text: choiceButton.modelData.label
                    icon.name: choiceButton.modelData.icon
                    checked: pickerDialog.selectedKey === choiceButton.modelData.key
                    onToggled: if (checked) pickerDialog.selectedKey = choiceButton.modelData.key
                    Layout.fillWidth: true
                }
            }
        }
    }

    Kirigami.Dialog {
        id: noTargetDialog
        objectName: "fileDropNoTarget"
        title: dropZone.dropState.title || ""
        standardButtons: Kirigami.Dialog.Close
        preferredWidth: Kirigami.Units.gridUnit * 24
        onClosed: if (dropZone.phase === "none") dropZone.bridge.cancelDrop()
        customFooterActions: [
            Kirigami.Action {
                text: dropZone.dropState.settingsLabel || ""
                icon.name: "preferences-plugin"
                onTriggered: {
                    dropZone.bridge.cancelDrop()
                    dropZone.pluginSettingsRequested()
                }
            }
        ]

        Controls.Label {
            objectName: "fileDropNoTargetBody"
            text: dropZone.dropState.body || ""
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
        }
    }
}
