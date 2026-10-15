import QtQuick
import QtQuick.Controls as Controls
import org.kde.kirigami as Kirigami
import org.kde.kirigami.delegates as KD

// One quick-settings row in the phone card: a switch, its title and a
// wrapped, dimmed explanation, laid out like Kirigami's subtitle delegates.
Controls.SwitchDelegate {
    id: row

    property string subtitle: ""

    horizontalPadding: Kirigami.Units.largeSpacing
    topPadding: Kirigami.Units.smallSpacing
    bottomPadding: Kirigami.Units.smallSpacing

    contentItem: KD.TitleSubtitle {
        title: row.text
        subtitle: row.subtitle
        textFormat: Text.PlainText
        wrapMode: Text.Wrap
        elide: Text.ElideNone
        opacity: row.enabled ? 1 : 0.6
    }
}
