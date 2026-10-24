pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import "ui"

// Newest photos from a photos plugin (PLUGINS.md), loaded only while this
// tab is shown. Without a plugin, or before its setup, the tab shows a dimmed
// hint with the setup command instead. Labels are plain text; images are
// local cache files the bridge has already checked.
ColumnLayout {
    id: tab
    objectName: "photosTab"
    required property var bridge
    readonly property var photos: tab.bridge.photos || ({})
    property string filter: "all"
    readonly property var items: (tab.photos.items || []).filter(item =>
        tab.filter === "all" || (tab.filter === "videos") === (item.video === true))
    spacing: 0

    SectionHeader {
        text: qsTr("Recent Photos")
        FilterBar {
            objectName: "photosFilter"
            visible: tab.photos.ready === true
            options: [
                { "key": "all", "label": qsTr("All") },
                { "key": "photos", "label": qsTr("Photos") },
                { "key": "videos", "label": qsTr("Videos") }
            ]
            current: tab.filter
            onPicked: key => tab.filter = key
        }
        Controls.ToolButton {
            objectName: "photosRefresh"
            icon.name: "view-refresh"
            text: qsTr("Refresh")
            display: Controls.AbstractButton.IconOnly
            enabled: tab.photos.present === true
            Accessible.name: text
            Controls.ToolTip.visible: hovered
            Controls.ToolTip.text: text
            onClicked: tab.bridge.refreshPhotos()
        }
    }
    Controls.Label {
        objectName: "photosSummary"
        Layout.fillWidth: true
        Layout.leftMargin: Kirigami.Units.largeSpacing
        Layout.rightMargin: Kirigami.Units.largeSpacing
        visible: tab.photos.ready === true
        text: tab.photos.hint || ""
        textFormat: Text.PlainText
        elide: Text.ElideRight
        font: Kirigami.Theme.smallFont
        color: Kirigami.Theme.disabledTextColor
    }

    Item {
        Layout.fillWidth: true
        Layout.fillHeight: true
        visible: tab.photos.loaded === true && (tab.photos.ready !== true || tab.items.length === 0)
        EmptyState {
            anchors.centerIn: parent
            objectName: "photosPlaceholder"
            dimmed: tab.photos.ready !== true
            icon.name: "folder-pictures"
            text: tab.photos.ready === true ? (tab.filter === "videos" ? qsTr("No videos") : qsTr("No photos"))
                : tab.photos.present === true ? qsTr("Photos are not available") : qsTr("No photo plugin")
            explanation: tab.photos.ready === true ? "" : (tab.photos.hint || "")
        }
    }

    Controls.BusyIndicator {
        Layout.alignment: Qt.AlignCenter
        Layout.fillHeight: true
        visible: tab.photos.loaded !== true
        running: visible
    }

    Controls.ScrollView {
        Layout.fillWidth: true
        Layout.fillHeight: true
        Layout.leftMargin: Kirigami.Units.smallSpacing
        visible: tab.photos.ready === true && tab.items.length > 0
        Controls.ScrollBar.horizontal.policy: Controls.ScrollBar.AlwaysOff

        GridView {
            id: grid
            objectName: "photosGrid"
            clip: true
            activeFocusOnTab: true
            keyNavigationEnabled: true
            currentIndex: -1
            Keys.onReturnPressed: if (currentIndex >= 0) tab.bridge.openPhoto(tab.items[currentIndex].id)
            Keys.onEnterPressed: if (currentIndex >= 0) tab.bridge.openPhoto(tab.items[currentIndex].id)
            model: tab.items
            cellWidth: Kirigami.Units.gridUnit * 9
            cellHeight: Kirigami.Units.gridUnit * 10

            delegate: Item {
                id: cell
                required property var modelData
                required property int index
                width: grid.cellWidth
                height: grid.cellHeight

                Rectangle {
                    id: frame
                    anchors.fill: parent
                    anchors.margins: Kirigami.Units.smallSpacing
                    radius: Kirigami.Units.cornerRadius
                    // Same tint and focus line as the list rows.
                    color: hover.hovered ? Qt.alpha(Kirigami.Theme.highlightColor, 0.1) : "transparent"
                    border.width: grid.activeFocus && grid.currentIndex === cell.index ? 1 : 0
                    border.color: Kirigami.Theme.focusColor

                    Image {
                        id: thumbnail
                        anchors.top: parent.top
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.margins: Kirigami.Units.smallSpacing
                        height: parent.height - caption.height - Kirigami.Units.largeSpacing
                        source: cell.modelData.thumbnail
                        sourceSize.width: 256
                        sourceSize.height: 256
                        fillMode: Image.PreserveAspectCrop
                        asynchronous: true
                        cache: false
                    }
                    Kirigami.Icon {
                        anchors.centerIn: thumbnail
                        width: Kirigami.Units.iconSizes.medium
                        height: width
                        visible: thumbnail.status !== Image.Ready || cell.modelData.video === true
                        source: cell.modelData.video === true ? "media-playback-start" : "image-x-generic"
                    }
                    Controls.Label {
                        id: caption
                        anchors.bottom: parent.bottom
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.margins: Kirigami.Units.smallSpacing
                        text: cell.modelData.label
                        textFormat: Text.PlainText
                        elide: Text.ElideRight
                        horizontalAlignment: Text.AlignHCenter
                        font: Kirigami.Theme.smallFont
                    }

                    HoverHandler { id: hover }
                    TapHandler {
                        onTapped: tab.bridge.openPhoto(cell.modelData.id)
                    }
                    // Drag the original into other apps once it is downloaded
                    // (opening a photo downloads it).
                    DragHandler {
                        id: drag
                        enabled: cell.modelData.original !== ""
                        target: null
                    }
                    Drag.active: drag.active
                    Drag.dragType: Drag.Automatic
                    Drag.supportedActions: Qt.CopyAction
                    Drag.mimeData: ({"text/uri-list": cell.modelData.original})

                    Controls.ToolTip.visible: hover.hovered
                    Controls.ToolTip.delay: Kirigami.Units.toolTipDelay
                    Controls.ToolTip.text: cell.modelData.original !== ""
                        ? qsTr("Click to open, or drag the file into another app")
                        : qsTr("Click to download and open")
                }
            }
        }
    }
}
