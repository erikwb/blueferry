pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

// Settings behind the gear: categories on the left, like KDE's System
// Settings. In a narrow window the categories are a list and a category
// opens on its own (drill-down) with a back button.
Kirigami.Page {
    id: settingsPage
    objectName: "phoneSettingsPage"
    title: qsTr("Settings")
    padding: 0
    required property var bridge
    signal closeRequested()
    signal clearHistoryRequested()
    signal shortcutsRequested()
    signal aboutRequested()
    signal bluetoothRestartRequested()
    signal pairingIssueRequested()
    signal forgetRequested(string mac)
    signal storagePolicyRequested(string policy)
    signal pairingRequested(string mac, bool paired, bool compatibilityMode, bool explicitPairing)

    property string category: "phone"
    property bool drilled: false
    readonly property bool narrow: width < Kirigami.Units.gridUnit * 34
    readonly property var categories: [
        { "key": "phone", "name": qsTr("Phone"), "icon": "smartphone" },
        { "key": "notifications", "name": qsTr("Notifications"), "icon": "preferences-desktop-notification" },
        { "key": "calls", "name": qsTr("Calls & Audio"), "icon": "call-start" },
        { "key": "security", "name": qsTr("Security"), "icon": "preferences-security" },
        { "key": "plugins", "name": qsTr("Plugins"), "icon": "preferences-plugin" },
        { "key": "about", "name": qsTr("About"), "icon": "help-about" }
    ]
    readonly property bool restartPending: (settingsPage.bridge.features || ({})).restart_pending === true

    function categoryName(key) {
        for (const entry of settingsPage.categories) {
            if (entry.key === key)
                return entry.name
        }
        return ""
    }

    function open(key) {
        settingsPage.category = key
        settingsPage.drilled = true
    }

    Component.onCompleted: settingsPage.bridge.loadFeatures()

    actions: [
        Kirigami.Action {
            objectName: "settingsBackAction"
            text: qsTr("All Settings")
            icon.name: "go-previous"
            visible: settingsPage.narrow && settingsPage.drilled
            onTriggered: settingsPage.drilled = false
        },
        Kirigami.Action {
            text: qsTr("Close Settings")
            icon.name: "window-close"
            displayHint: Kirigami.DisplayHint.IconOnly
            onTriggered: settingsPage.closeRequested()
        }
    ]

    RowLayout {
        anchors.fill: parent
        spacing: 0

        Controls.ScrollView {
            objectName: "settingsCategories"
            Layout.fillHeight: true
            Layout.fillWidth: settingsPage.narrow
            Layout.preferredWidth: settingsPage.narrow ? -1 : Kirigami.Units.gridUnit * 12
            visible: !settingsPage.narrow || !settingsPage.drilled

            ListView {
                id: categoryList
                model: settingsPage.categories
                clip: true
                delegate: Controls.ItemDelegate {
                    id: categoryItem
                    required property var modelData
                    objectName: "category_" + categoryItem.modelData.key
                    width: ListView.view.width
                    text: categoryItem.modelData.name
                    icon.name: categoryItem.modelData.icon
                    highlighted: !settingsPage.narrow
                        && settingsPage.category === categoryItem.modelData.key
                    onClicked: settingsPage.open(categoryItem.modelData.key)
                }
            }
        }

        Kirigami.Separator {
            Layout.fillHeight: true
            visible: !settingsPage.narrow
        }

        Controls.ScrollView {
            id: detail
            objectName: "settingsDetail"
            Layout.fillWidth: true
            Layout.fillHeight: true
            visible: !settingsPage.narrow || settingsPage.drilled
            contentWidth: availableWidth
            Controls.ScrollBar.horizontal.policy: Controls.ScrollBar.AlwaysOff

            ColumnLayout {
                width: detail.availableWidth
                spacing: Kirigami.Units.largeSpacing

                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.margins: Kirigami.Units.largeSpacing
                    spacing: Kirigami.Units.largeSpacing

                    Kirigami.Heading {
                        text: settingsPage.categoryName(settingsPage.category)
                        level: 1
                    }
                    Kirigami.InlineMessage {
                        Layout.fillWidth: true
                        visible: settingsPage.bridge.errorText !== ""
                        text: String(settingsPage.bridge.errorText || "")
                            .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
                        type: Kirigami.MessageType.Error
                    }
                    Kirigami.InlineMessage {
                        objectName: "restartNotice"
                        Layout.fillWidth: true
                        visible: settingsPage.restartPending
                        type: Kirigami.MessageType.Information
                        text: (settingsPage.bridge.features || ({})).restart_notice || ""
                        actions: [
                            Kirigami.Action {
                                text: qsTr("Restart Service")
                                icon.name: "system-reboot"
                                enabled: !settingsPage.bridge.busy
                                onTriggered: settingsPage.bridge.restartBackend()
                            }
                        ]
                    }

                    Loader {
                        id: categoryLoader
                        objectName: "categoryLoader"
                        Layout.fillWidth: true
                        sourceComponent: settingsPage.category === "notifications" ? notificationsComponent
                            : settingsPage.category === "calls" ? callsComponent
                            : settingsPage.category === "security" ? securityComponent
                            : settingsPage.category === "plugins" ? pluginsComponent
                            : settingsPage.category === "about" ? aboutComponent
                            : phoneComponent
                    }
                }
            }
        }
    }

    Component {
        id: phoneComponent
        SettingsPhone {
            bridge: settingsPage.bridge
            onBluetoothRestartRequested: settingsPage.bluetoothRestartRequested()
            onPairingIssueRequested: settingsPage.pairingIssueRequested()
            onForgetRequested: mac => settingsPage.forgetRequested(mac)
            onPairingRequested: (mac, paired, compatibilityMode, explicitPairing) =>
                settingsPage.pairingRequested(mac, paired, compatibilityMode, explicitPairing)
        }
    }
    Component {
        id: notificationsComponent
        SettingsNotifications { bridge: settingsPage.bridge }
    }
    Component {
        id: callsComponent
        SettingsCallsAudio { bridge: settingsPage.bridge }
    }
    Component {
        id: securityComponent
        SettingsSecurity {
            bridge: settingsPage.bridge
            onStoragePolicyRequested: policy => settingsPage.storagePolicyRequested(policy)
            onClearHistoryRequested: settingsPage.clearHistoryRequested()
        }
    }
    Component {
        id: pluginsComponent
        SettingsPlugins { bridge: settingsPage.bridge }
    }
    Component {
        id: aboutComponent
        SettingsAbout {
            bridge: settingsPage.bridge
            onShortcutsRequested: settingsPage.shortcutsRequested()
            onAboutRequested: settingsPage.aboutRequested()
        }
    }
}
