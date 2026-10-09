pragma ComponentBehavior: Bound

import QtQuick
import org.kde.kirigami as Kirigami

// Conversation avatar: the theme icon, with the contact's photo on top when
// the opt-in contact photos option is on and the backend has one. The photo
// is decoded by the controller's image provider, never by the daemon.
Item {
    id: avatar

    required property var bridge
    property string address: ""
    property bool group: false
    property bool photoReady: false

    readonly property bool photosEnabled: !group && address !== ""
        && bridge.status.contact_photos === true
    // avatarRevision only makes this binding re-read avatarSource(); the URL
    // of an avatar already shown stays the same, so it is not reloaded.
    readonly property string photoSource: photosEnabled && bridge.avatarRevision >= 0
        ? bridge.avatarSource(address) : ""

    implicitWidth: Kirigami.Units.iconSizes.smallMedium
    implicitHeight: implicitWidth

    Kirigami.Icon {
        anchors.fill: parent
        visible: !avatar.photoReady
        source: avatar.group ? "system-users" : "user-identity"
    }

    Loader {
        anchors.fill: parent
        active: avatar.photoSource !== ""
        onActiveChanged: if (!active) avatar.photoReady = false
        sourceComponent: Image {
            source: avatar.photoSource
            sourceSize.width: Math.ceil(avatar.width * Screen.devicePixelRatio)
            sourceSize.height: Math.ceil(avatar.height * Screen.devicePixelRatio)
            fillMode: Image.PreserveAspectCrop
            asynchronous: true
            cache: false
            smooth: true
            visible: status === Image.Ready
            onStatusChanged: avatar.photoReady = status === Image.Ready
        }
    }
}
