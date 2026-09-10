pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

Kirigami.ScrollablePage {
    id: iphonePage
    objectName: "phoneSettingsPage"
    title: qsTr("iPhone Settings")
    required property var bridge
    signal closeRequested()
    signal clearHistoryRequested()
    signal bluetoothRestartRequested()
    signal pairingIssueRequested()
    signal forgetRequested(string mac)
    signal storagePolicyRequested(string policy)
    signal pairingRequested(string mac, bool paired, bool compatibilityMode, bool explicitPairing)

    function htmlEscape(value) {
        return String(value || "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
    }

    function storageStatusText() {
        const status = bridge.status || ({})
        if (status.daemon !== true)
            return qsTr("Unavailable")
        if (status.storage_policy === "none")
            return qsTr("Disabled")
        if (status.storage_state === "ready")
            return qsTr("Available")
        if (status.storage_state === "locked")
            return qsTr("Locked")
        return qsTr("Unavailable")
    }

    property int selectedDevice: -1
    property var device: selectedDevice >= 0 && selectedDevice < iphonePage.bridge.devices.length
        ? iphonePage.bridge.devices[selectedDevice]
        : null
    property bool hasMultipleAdapters: (iphonePage.bridge.compatibility.adapters || []).length > 1

    function syncAdapterCombo() {
        const adapters = iphonePage.bridge.compatibility.adapters || []
        const current = iphonePage.bridge.compatibility.adapter
        for (let index = 0; index < adapters.length; ++index) {
            if (adapters[index].name === current) {
                adapterCombo.currentIndex = index
                return
            }
        }
    }
    property var configuredDevice: {
        for (let index = 0; index < iphonePage.bridge.devices.length; ++index) {
            if (iphonePage.bridge.devices[index].mac === iphonePage.bridge.configuredMac)
                return iphonePage.bridge.devices[index]
        }
        return null
    }
    property string effectiveStage: iphonePage.bridge.onboardingStage
    property bool compatibilityModeOverride: false
    property var explicitPairingOverrides: ({})

    ColumnLayout {
        width: parent.width
        spacing: Kirigami.Units.largeSpacing

        RowLayout {
            Layout.fillWidth: true

            Item { Layout.fillWidth: true }
            Controls.ToolButton {
                icon.name: "window-close"
                text: qsTr("Close Settings")
                Accessible.name: text
                Controls.ToolTip.text: text
                Controls.ToolTip.visible: hovered
                onClicked: iphonePage.closeRequested()
            }
        }

        Kirigami.InlineMessage {
            Layout.fillWidth: true
            visible: iphonePage.bridge.errorText !== ""
            text: iphonePage.htmlEscape(iphonePage.bridge.errorText)
            type: Kirigami.MessageType.Error
        }

        Kirigami.Heading {
            text: iphonePage.bridge.configured
                ? qsTr("Your iPhone") : qsTr("Connect an iPhone")
            level: 2
        }
        Controls.Label {
            Layout.fillWidth: true
            wrapMode: Text.Wrap
            visible: !iphonePage.bridge.configured
            text: qsTr("Keep the iPhone unlocked with its Bluetooth settings open during pairing.")
        }

        Connections {
            target: iphonePage.bridge
            function onCompatibilityChanged() { iphonePage.syncAdapterCombo() }
            function onDevicesChanged() {
                deviceCombo.currentIndex = iphonePage.bridge.devices.length > 0 ? 0 : -1
                iphonePage.selectedDevice = deviceCombo.currentIndex
            }
        }

        OnboardingSummary {
            id: onboardingSummary
            Layout.fillWidth: true
            stage: compatibilityMode.checked
                && iphonePage.effectiveStage === "activate-bluetooth"
                ? "select-device" : iphonePage.effectiveStage
            compatibility: iphonePage.bridge.onboardingCompatibility
            status: iphonePage.bridge.status
            storagePolicy: iphonePage.bridge.status.storage_policy || ""
            storageState: iphonePage.bridge.status.storage_state || ""
        }

        Kirigami.FormLayout {
            Layout.fillWidth: true
            visible: !iphonePage.bridge.configured

            Controls.Label {
                Kirigami.FormData.label: qsTr("Controller:")
                visible: !iphonePage.hasMultipleAdapters
                text: iphonePage.bridge.compatibility.adapter || qsTr("Checking…")
                textFormat: Text.PlainText
            }

            Controls.ComboBox {
                id: adapterCombo
                objectName: "adapterSelector"
                Kirigami.FormData.label: qsTr("Controller:")
                visible: iphonePage.hasMultipleAdapters
                model: iphonePage.bridge.compatibility.adapters || []
                textRole: "label"
                valueRole: "name"
                enabled: !iphonePage.bridge.busy
                onActivated: {
                    if (currentValue)
                        iphonePage.bridge.selectAdapter(currentValue)
                }
                Component.onCompleted: iphonePage.syncAdapterCombo()
            }

            Controls.Label {
                Kirigami.FormData.label: qsTr("Hardware:")
                text: !iphonePage.bridge.compatibilityLoaded
                    ? qsTr("Checking…")
                    : iphonePage.bridge.compatibility.available !== true
                    ? qsTr("Could Not Verify — Pairing Still Available")
                    : iphonePage.bridge.compatibility.hardware_supported
                        ? qsTr("Compatible")
                        : qsTr("Incompatible")
            }

            Controls.Label {
                Kirigami.FormData.label: qsTr("Messages and Contacts:")
                text: iphonePage.bridge.compatibility.messages_supported
                    ? qsTr("Supported") : qsTr("Not Detected")
            }

            Controls.Label {
                Kirigami.FormData.label: qsTr("iPhone Notifications:")
                text: iphonePage.bridge.compatibility.notifications_supported
                    ? qsTr("Supported") : qsTr("Unavailable")
            }

            Controls.Label {
                Kirigami.FormData.label: qsTr("Bluetooth Support:")
                text: compatibilityMode.checked
                    || !iphonePage.bridge.compatibility.notifications_supported
                    ? qsTr("Not Required")
                    : iphonePage.bridge.bluetoothActive
                        ? qsTr("Active")
                        : qsTr("Restart Required")
            }
        }

        RowLayout {
            visible: !iphonePage.bridge.configured
            Controls.Button {
                visible: iphonePage.bridge.compatibility.notifications_supported === true
                    && !iphonePage.bridge.bluetoothActive
                    && !compatibilityMode.checked
                text: qsTr("Restart Bluetooth")
                icon.name: "network-bluetooth"
                enabled: !iphonePage.bridge.busy
                onClicked: iphonePage.bluetoothRestartRequested()
            }
            Controls.Button {
                text: qsTr("1. Scan for iPhone")
                icon.name: "edit-find"
                enabled: !iphonePage.bridge.busy
                onClicked: iphonePage.bridge.loadDevices(true)
            }
            Controls.BusyIndicator {
                running: iphonePage.bridge.busy
                visible: running
            }
        }

        Kirigami.FormLayout {
            Layout.fillWidth: true
            visible: !iphonePage.bridge.configured

            Controls.ComboBox {
                id: deviceCombo
                objectName: "phoneSelector"
                Kirigami.FormData.label: qsTr("Found iPhone:")
                model: iphonePage.bridge.devices
                textRole: "display_name"
                valueRole: "mac"
                enabled: !iphonePage.bridge.busy
                // org.kde.desktop paints displayText in the StyleItem
                // background. A contentItem Label would overlay a second copy.
                delegate: Controls.ItemDelegate {
                    id: deviceOption
                    required property var modelData
                    width: deviceCombo.width
                    Accessible.name: deviceOption.modelData.display_name
                    contentItem: Controls.Label {
                        text: deviceOption.modelData.display_name
                        textFormat: Text.PlainText
                        elide: Text.ElideRight
                    }
                }
                onCurrentIndexChanged: iphonePage.selectedDevice = currentIndex
            }
        }

        Controls.CheckBox {
            id: compatibilityMode
            Layout.fillWidth: true
            visible: !iphonePage.bridge.configured
            text: qsTr("Compatibility pairing for iOS 18 or earlier")
            checked: iphonePage.bridge.compatibilityLoaded
                && (iphonePage.bridge.compatibility.notifications_supported !== true
                    || iphonePage.compatibilityModeOverride)
            enabled: iphonePage.bridge.compatibilityLoaded
                && iphonePage.bridge.compatibility.notifications_supported === true
                && !iphonePage.bridge.busy
            onClicked: iphonePage.compatibilityModeOverride = checked
            Accessible.description: qsTr("Sets up Messages and Contacts without connecting ANCS.")
        }

        Controls.CheckBox {
            id: explicitPairing
            objectName: "explicitPairingCheckBox"
            Layout.fillWidth: true
            visible: !iphonePage.bridge.configured
            text: qsTr("Use explicit Bluetooth pairing")
            checked: iphonePage.explicitPairingOverrides[iphonePage.bridge.compatibility.adapter]
                ?? (iphonePage.bridge.compatibility.explicit_pairing_default === true)
            enabled: iphonePage.bridge.compatibilityLoaded && !iphonePage.bridge.busy
            onClicked: {
                const overrides = Object.assign({}, iphonePage.explicitPairingOverrides)
                overrides[iphonePage.bridge.compatibility.adapter] = checked
                iphonePage.explicitPairingOverrides = overrides
            }
            Accessible.description: qsTr("Skips the initial Bluetooth connection attempt and calls Pair immediately. Try this for controllers that cancel normal pairing.")
        }

        RowLayout {
            visible: !iphonePage.bridge.configured
            Controls.Button {
                objectName: "pairPhoneButton"
                text: iphonePage.device !== null && iphonePage.device.paired
                    ? qsTr("Use Existing Pairing") : qsTr("2. Pair Selected iPhone")
                icon.name: "network-connect"
                enabled: iphonePage.device !== null
                    && iphonePage.bridge.compatibilityLoaded
                    && iphonePage.bridge.compatibility.pairing_ready !== false
                    && !iphonePage.bridge.busy
                onClicked: {
                    iphonePage.pairingRequested(
                        iphonePage.device.mac, iphonePage.device.paired,
                        compatibilityMode.checked, explicitPairing.checked
                    )
                }
            }
            Controls.Button {
                text: qsTr("Forget")
                icon.name: "edit-delete-remove"
                enabled: iphonePage.device !== null
                    && iphonePage.device.paired && !iphonePage.bridge.busy
                onClicked: {
                    iphonePage.forgetRequested(iphonePage.device.mac)
                }
            }
        }

        Controls.Label {
            Layout.fillWidth: true
            visible: !iphonePage.bridge.configured && compatibilityMode.checked
                && iphonePage.bridge.compatibility.pairing_ready !== false
            wrapMode: Text.Wrap
            text: qsTr("BlueFerry will still advertise ANCS solicitation so the iPhone exposes its Messages and Contacts permissions, but it will not connect system notifications.")
        }

        Controls.Button {
            visible: iphonePage.bridge.pairingIssueReport !== ""
            text: qsTr("Report Pairing Issue")
            icon.name: "help-about"
            onClicked: iphonePage.pairingIssueRequested()
        }

        RowLayout {
            Layout.fillWidth: true
            visible: iphonePage.bridge.configured

            Controls.Label {
                Layout.fillWidth: true
                text: iphonePage.configuredDevice !== null
                    ? iphonePage.configuredDevice.name
                    : qsTr("iPhone")
                textFormat: Text.PlainText
            }
            Controls.Button {
                text: qsTr("Unpair")
                icon.name: "network-disconnect"
                enabled: !iphonePage.bridge.busy
                onClicked: {
                    iphonePage.forgetRequested(iphonePage.bridge.configuredMac)
                }
            }
        }

        Kirigami.Heading { text: qsTr("Connection Health"); level: 2 }
        Kirigami.FormLayout {
            Layout.fillWidth: true

            Controls.Label {
                Kirigami.FormData.label: qsTr("Background Service:")
                text: iphonePage.bridge.status.daemon ? qsTr("Running") : qsTr("Unavailable")
            }
            Controls.Label {
                Kirigami.FormData.label: qsTr("Messages:")
                Layout.fillWidth: iphonePage.bridge.status.map_connection_refused === true
                wrapMode: Text.Wrap
                text: iphonePage.bridge.status.map_connection_refused === true
                    ? qsTr("iPhone is refusing message connections; is it connected to another computer?")
                    : iphonePage.bridge.status.map ? qsTr("Connected") : qsTr("Unavailable")
            }
            Controls.Label {
                Kirigami.FormData.label: qsTr("Contacts:")
                text: iphonePage.bridge.status.pbap ? qsTr("Connected") : qsTr("Unavailable")
            }
            Controls.Label {
                Kirigami.FormData.label: qsTr("iPhone Notifications:")
                text: iphonePage.bridge.status.ancs ? qsTr("Connected") : qsTr("Unavailable")
            }
            Controls.Label {
                Kirigami.FormData.label: qsTr("Contact Destinations:")
                text: iphonePage.bridge.status.contacts || "0"
            }
        }
        Kirigami.InlineMessage {
            Layout.fillWidth: true
            visible: iphonePage.bridge.configured === true
                && iphonePage.bridge.onboardingCompatibility.notifications_supported === true
                && iphonePage.bridge.status.map === true
                && iphonePage.bridge.status.pbap === true
                && iphonePage.bridge.status.ancs === false
            type: onboardingSummary.ancsLimitedController()
                ? Kirigami.MessageType.Positive
                : Kirigami.MessageType.Information
            text: onboardingSummary.ancsUnavailableHint()
        }

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
                currentIndex: iphonePage.bridge.status.notification_policy === "all" ? 0
                    : iphonePage.bridge.status.notification_policy === "none" ? 2 : 1
                enabled: iphonePage.bridge.status.daemon === true && !iphonePage.bridge.busy
                onActivated: iphonePage.bridge.setNotificationPolicy(currentValue)
            }
            Controls.CheckBox {
                Layout.fillWidth: true
                text: qsTr("Only notify for contacts")
                checked: iphonePage.bridge.status.contacts_only_notifications === true
                enabled: iphonePage.bridge.status.daemon === true
                    && iphonePage.bridge.status.notification_policy !== "none"
                    && !iphonePage.bridge.busy
                onClicked: iphonePage.bridge.setContactsOnlyNotifications(checked)
                Accessible.description: qsTr("Unknown senders remain available in message history.")
            }
        }

        Kirigami.Heading { text: qsTr("Local Data"); level: 2 }
        Kirigami.FormLayout {
            Layout.fillWidth: true

            Controls.ComboBox {
                objectName: "storagePolicySelector"
                Kirigami.FormData.label: qsTr("Storage:")
                textRole: "text"
                valueRole: "value"
                model: [
                    { "text": qsTr("Encrypted with Desktop Keyring"), "value": "encrypted" },
                    { "text": qsTr("Unencrypted Local Data"), "value": "plaintext" },
                    { "text": qsTr("Do Not Retain Local Data"), "value": "none" }
                ]
                currentIndex: iphonePage.bridge.status.storage_policy === "plaintext" ? 1
                    : iphonePage.bridge.status.storage_policy === "none" ? 2 : 0
                enabled: iphonePage.bridge.status.daemon === true && !iphonePage.bridge.busy
                onActivated: {
                    iphonePage.storagePolicyRequested(currentValue)
                }
            }

            Controls.Label {
                objectName: "storageStatusLabel"
                Kirigami.FormData.label: qsTr("Status:")
                Layout.fillWidth: true
                text: iphonePage.storageStatusText()
                textFormat: Text.PlainText
            }

            Controls.Label {
                Kirigami.FormData.label: qsTr("Details:")
                Layout.fillWidth: true
                visible: iphonePage.bridge.status.storage_detail !== undefined
                    && iphonePage.bridge.status.storage_detail !== ""
                text: iphonePage.bridge.status.storage_detail || ""
                textFormat: Text.PlainText
                wrapMode: Text.Wrap
            }

            Controls.Button {
                Kirigami.FormData.label: qsTr("Keyring:")
                visible: iphonePage.bridge.status.storage_policy === "encrypted"
                    && iphonePage.bridge.status.storage_state !== "ready"
                text: qsTr("Unlock Local Data")
                icon.name: "document-decrypt"
                enabled: iphonePage.bridge.status.daemon === true && !iphonePage.bridge.busy
                onClicked: iphonePage.bridge.unlockStorage()
            }
        }

        Kirigami.Heading { text: qsTr("Maintenance"); level: 2 }
        RowLayout {
            Controls.Button {
                text: qsTr("Restart Service")
                icon.name: "system-reboot"
                enabled: !iphonePage.bridge.busy
                onClicked: iphonePage.bridge.restartBackend()
            }
            Controls.Button {
                text: qsTr("Sync Contacts")
                icon.name: "view-refresh"
                enabled: !iphonePage.bridge.busy
                onClicked: iphonePage.bridge.syncContacts()
            }
            Controls.Button {
                text: qsTr("Clear History")
                icon.name: "edit-clear-history"
                enabled: !iphonePage.bridge.busy
                onClicked: iphonePage.clearHistoryRequested()
            }
        }
    }
}
