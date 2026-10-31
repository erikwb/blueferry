import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

// One quick-settings row in the phone card: a switch, its title and a
// wrapped, dimmed explanation, laid out like Kirigami's subtitle delegates.
Controls.SwitchDelegate {
    id: row

    property string subtitle: ""

    horizontalPadding: Kirigami.Units.largeSpacing
    topPadding: Kirigami.Units.smallSpacing
    bottomPadding: Kirigami.Units.smallSpacing

    Accessible.description: row.subtitle

    // TitleSubtitle.textFormat is missing from older supported Kirigami
    // versions. Labels keep reports and backend hints plain text everywhere.
    contentItem: ColumnLayout {
        spacing: 0
        opacity: row.enabled ? 1 : 0.6

        Controls.Label {
            Layout.fillWidth: true
            text: row.text
            font: row.font
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            elide: Text.ElideNone
            Accessible.ignored: true
        }
        Controls.Label {
            Layout.fillWidth: true
            visible: row.subtitle !== ""
            text: row.subtitle
            font: Kirigami.Theme.smallFont
            color: Kirigami.Theme.disabledTextColor
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            elide: Text.ElideNone
            Accessible.ignored: true
        }
    }
}
