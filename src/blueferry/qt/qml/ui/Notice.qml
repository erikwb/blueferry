import QtQuick
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

// An InlineMessage for remote or composed text: plainText is escaped, so
// nothing a phone or plugin sends is read as markup. Visible while there is
// text unless visible is bound otherwise.
Kirigami.InlineMessage {
    id: notice
    property string plainText: ""
    Layout.fillWidth: true
    visible: notice.plainText !== ""
    text: String(notice.plainText || "")
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
}
