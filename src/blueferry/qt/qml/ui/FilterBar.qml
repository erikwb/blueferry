pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as Controls

// Segmented filter like "All | Missed" in Recent Calls. options is a list of
// {key, label, objectName?}; the bar shows current and emits picked(key).
// Arrow keys move between the segments.
Row {
    id: bar
    property var options: []
    property string current: ""
    signal picked(string key)
    spacing: 0
    Accessible.role: Accessible.PageTabList

    Repeater {
        model: bar.options
        delegate: Controls.Button {
            id: segment
            required property var modelData
            required property int index
            objectName: segment.modelData.objectName || ("filter_" + segment.modelData.key)
            text: segment.modelData.label
            checkable: true
            checked: bar.current === segment.modelData.key
            autoExclusive: true
            Accessible.role: Accessible.PageTab
            onClicked: bar.picked(segment.modelData.key)
            Keys.onLeftPressed: bar.step(-1)
            Keys.onRightPressed: bar.step(1)
        }
    }

    function step(delta: int): void {
        const keys = bar.options.map(option => option.key)
        const index = keys.indexOf(bar.current)
        const next = keys[Math.max(0, Math.min(keys.length - 1, index + delta))]
        if (next !== undefined && next !== bar.current)
            bar.picked(next)
    }
}
