pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

Controls.ScrollView {
    id: messageComposer

    property alias text: editor.text
    property alias placeholderText: editor.placeholderText
    signal accepted()

    Layout.fillWidth: true
    Layout.minimumWidth: 0
    Layout.minimumHeight: Kirigami.Units.gridUnit * 2.5
    Layout.preferredHeight: Math.min(
        Math.max(
            editor.contentHeight + editor.topPadding + editor.bottomPadding + 2,
            Layout.minimumHeight
        ),
        Layout.maximumHeight
    )
    Layout.maximumHeight: Kirigami.Units.gridUnit * 8
    clip: true
    Controls.ScrollBar.horizontal.policy: Controls.ScrollBar.AlwaysOff
    Controls.ScrollBar.vertical.policy: Controls.ScrollBar.AsNeeded

    function clear() {
        editor.clear()
    }

    function forceActiveFocus() {
        editor.forceActiveFocus()
    }

    function submit(event) {
        if ((event.modifiers & Qt.ShiftModifier) !== 0) {
            event.accepted = false
            return
        }
        accepted()
        event.accepted = true
    }

    Controls.TextArea {
        id: editor
        width: messageComposer.availableWidth
        wrapMode: TextEdit.Wrap
        selectByMouse: true
        Accessible.name: messageComposer.Accessible.name
        Keys.onReturnPressed: event => messageComposer.submit(event)
        Keys.onEnterPressed: event => messageComposer.submit(event)
    }
}
