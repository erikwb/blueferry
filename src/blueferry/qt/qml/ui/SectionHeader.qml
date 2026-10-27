import QtQuick
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

// The heading of a page part, the way the Settings categories and the Calls
// tab show it: a Kirigami heading on the window background (no tool bar),
// the standard margins, and trailing controls (filter, refresh) on the
// right. level 2 heads a tab section, level 4 a phone card section.
RowLayout {
    id: header
    property alias text: heading.text
    property int level: 2
    // Card sections sit closer to their rows than tab sections.
    readonly property bool cardSection: header.level >= 4
    default property alias trailing: trailingRow.data
    // Set when the space is too narrow for title and controls: the controls
    // win, the tab already names the page.
    property bool hideText: false

    Layout.fillWidth: true
    Layout.leftMargin: Kirigami.Units.largeSpacing
    Layout.rightMargin: header.cardSection ? Kirigami.Units.smallSpacing : Kirigami.Units.largeSpacing
    Layout.topMargin: Kirigami.Units.largeSpacing
    Layout.bottomMargin: Kirigami.Units.smallSpacing
    spacing: Kirigami.Units.smallSpacing

    Kirigami.Heading {
        id: heading
        Layout.fillWidth: true
        visible: !header.hideText
        level: header.level
        textFormat: Text.PlainText
        elide: Text.ElideRight
        Accessible.role: Accessible.Heading
    }
    RowLayout {
        id: trailingRow
        spacing: Kirigami.Units.smallSpacing
    }
}
