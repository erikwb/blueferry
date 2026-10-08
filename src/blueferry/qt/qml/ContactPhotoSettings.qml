pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

// Opt-in for contact photos as avatars. Off by default; the daemon saves
// the choice and applies it without a restart.
ColumnLayout {
    id: photoSettings
    objectName: "contactPhotoSettings"
    required property var bridge
    readonly property var status: bridge.status || ({})
    readonly property bool available: status.daemon === true && !bridge.busy

    Kirigami.Heading { text: qsTr("Contact Photos"); level: 2 }
    Kirigami.FormLayout {
        Layout.fillWidth: true

        Controls.CheckBox {
            objectName: "contactPhotosCheckBox"
            Layout.fillWidth: true
            text: qsTr("Show contact photos from the iPhone")
            checked: photoSettings.status.contact_photos === true
            enabled: photoSettings.available
            onClicked: photoSettings.bridge.setContactPhotos(checked)
            Accessible.description: photoCaption.text
        }
        Controls.Label {
            id: photoCaption
            objectName: "contactPhotosCaption"
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            font: Kirigami.Theme.smallFont
            text: qsTr("Photos are kept with your contacts and arrive with the next contact "
                + "sync. Turning this off erases them.")
        }
    }
}
