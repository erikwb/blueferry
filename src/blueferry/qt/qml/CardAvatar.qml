import QtQuick
import QtQuick.Controls as Controls
import org.kde.kirigami as Kirigami

// Avatar of a contact card: the photo when one is given (decoded by the
// controller's image provider), else the initials from contacts_view on a
// tinted circle, else the contact icon.
Item {
    id: avatar
    property string initials: ""
    property string source: ""
    readonly property bool photoReady: photo.status === Image.Ready

    implicitWidth: Kirigami.Units.iconSizes.medium
    implicitHeight: implicitWidth

    Rectangle {
        anchors.fill: parent
        radius: width / 2
        visible: !avatar.photoReady && avatar.initials !== ""
        color: Qt.alpha(Kirigami.Theme.highlightColor, 0.25)
        Controls.Label {
            anchors.centerIn: parent
            text: avatar.initials
            textFormat: Text.PlainText
            font.pixelSize: Math.max(8, avatar.height * 0.4)
            font.bold: true
        }
    }
    Kirigami.Icon {
        anchors.fill: parent
        visible: !avatar.photoReady && avatar.initials === ""
        source: "user-identity"
    }
    Image {
        id: photo
        anchors.fill: parent
        visible: avatar.photoReady
        source: avatar.source
        sourceSize.width: Math.ceil(avatar.width * Screen.devicePixelRatio)
        sourceSize.height: Math.ceil(avatar.height * Screen.devicePixelRatio)
        fillMode: Image.PreserveAspectCrop
        asynchronous: true
        cache: false
        smooth: true
    }
}
