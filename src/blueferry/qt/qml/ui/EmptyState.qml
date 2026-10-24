import QtQuick
import org.kde.kirigami as Kirigami

// What a list or tab shows when it has nothing: a centred icon, a title and
// one explanation, dimmed when the feature is off (Calls tab style). Inside a
// ListView set anchors.centerIn: parent; in a layout it fills the space.
Kirigami.PlaceholderMessage {
    id: empty
    // Off or unavailable rather than merely empty: drawn dimmed.
    property bool dimmed: false
    enabled: !empty.dimmed
    width: parent ? Math.min(parent.width - Kirigami.Units.gridUnit * 4,
                             Kirigami.Units.gridUnit * 26) : implicitWidth
}
