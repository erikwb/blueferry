pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

// Settings > Notifications: popup policy, content, ANCS action buttons,
// click rules and the recent-notification list.
ColumnLayout {
    id: section
    objectName: "settingsNotifications"
    required property var bridge
    spacing: Kirigami.Units.largeSpacing

    Kirigami.Heading { text: qsTr("Desktop Notifications"); level: 2 }
    Controls.Label {
        Layout.fillWidth: true
        wrapMode: Text.Wrap
        text: qsTr("Choose which iPhone events create desktop popups. Messages only is the default.")
    }
    Kirigami.FormLayout {
        Layout.fillWidth: true

        Controls.ComboBox {
            Kirigami.FormData.label: qsTr("Show Popups:")
            textRole: "text"
            valueRole: "value"
            model: [
                { "text": qsTr("All iPhone Notifications"), "value": "all" },
                { "text": qsTr("Messages Only"), "value": "messages" },
                { "text": qsTr("None"), "value": "none" }
            ]
            currentIndex: section.bridge.status.notification_policy === "all" ? 0
                : section.bridge.status.notification_policy === "none" ? 2 : 1
            enabled: section.bridge.status.daemon === true && !section.bridge.busy
            onActivated: section.bridge.setNotificationPolicy(currentValue)
        }
        Controls.CheckBox {
            Layout.fillWidth: true
            text: qsTr("Only notify for contacts")
            checked: section.bridge.status.contacts_only_notifications === true
            enabled: section.bridge.status.daemon === true
                && section.bridge.status.notification_policy !== "none"
                && !section.bridge.busy
            onClicked: section.bridge.setContactsOnlyNotifications(checked)
            Accessible.description: qsTr("Unknown senders remain available in message history.")
        }
    }
    // Click rules only apply to non-Messages popups, which exist only in
    // the "all" policy. Load the editor (and its backend read) on demand,
    // and only for daemons that report the rules' revision: an older,
    // not yet restarted backend lacks the methods.
    Loader {
        objectName: "notificationOpenMapLoader"
        Layout.fillWidth: true
        active: section.bridge.status.daemon === true
            && section.bridge.status.notification_policy === "all"
            && section.bridge.status.notification_open_map_revision !== undefined
        visible: active
        sourceComponent: Component {
            NotificationOpenMapEditor {
                bridge: section.bridge
            }
        }
    }

    Kirigami.Heading { text: qsTr("Content and Actions"); level: 2 }
    FeatureSwitch {
        Layout.fillWidth: true
        bridge: section.bridge
        feature: "show_notification_content"
        variable: "BLUEFERRY_SHOW_NOTIFICATION_CONTENT"
        text: qsTr("Show message content in popups")
        description: qsTr("Off: popups name the sender only.")
    }
    // Runtime opt-in (Messages1.SetAncsNotificationActions); only daemons
    // that report the preference support it.
    Controls.CheckBox {
        objectName: "ancsActionsCheckBox"
        Layout.fillWidth: true
        visible: section.bridge.status.ancs_actions_preference !== undefined
        text: qsTr("Show iPhone action buttons (Accept, Decline, Clear…)")
        checked: section.bridge.status.ancs_actions_preference === true
        // A saved "on" can always be switched off; switching on
        // needs All iPhone Notifications with content shown.
        enabled: section.bridge.status.daemon === true
            && !section.bridge.busy
            && (checked
                || (section.bridge.status.notification_policy === "all"
                    && section.bridge.status.notification_content_shown !== false))
        onClicked: section.bridge.setAncsNotificationActions(checked)
        Accessible.description: section.bridge.status.notification_content_shown === false
            ? qsTr("Unavailable while notification content is hidden.")
            : qsTr("Clicking a button runs that action on the iPhone, for example answering or declining a call. Applies to All iPhone Notifications.")
    }
    FeatureSwitch {
        Layout.fillWidth: true
        bridge: section.bridge
        feature: "mark_read_on_dismiss"
        variable: "BLUEFERRY_MARK_READ_ON_DISMISS"
        text: qsTr("Mark read when a popup is dismissed")
    }

    Kirigami.Heading { text: qsTr("History"); level: 2 }
    FeatureSwitch {
        Layout.fillWidth: true
        bridge: section.bridge
        feature: "notification_history"
        variable: "BLUEFERRY_NOTIFICATION_HISTORY"
        text: qsTr("Keep recent iPhone notifications")
        description: qsTr("Lists other apps' notifications in the Notifications tab (memory only).")
    }
}
