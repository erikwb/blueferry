pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
  id: root
  required property var ferryTheme
  required property var setup
  required property var status
  property var busy: ({})
  readonly property var theme: ferryTheme
  signal closeRequested
  signal pairingIssueRequested
  signal operationRequested(string method, var args)
  signal errorRequested(string message)
  signal reloadRequested

  OnboardingState {
    id: onboarding
    notificationsSupported: root.setup.notificationsSupported && root.setup.ancsEnabled && !root.setup.compatibilityModeOverride
    bluezActive: root.setup.bluezActive
    configured: root.setup.configured
    backendStatus: root.status
    pairingReady: root.setup.pairingReady
  }

  function ancsLimited() {
    return root.status.ancs_limited_controller === true || root.setup.ancsLimitedController;
  }

  function ancsVendorName() {
    return String(root.status.controller_vendor || root.setup.controllerVendor || "");
  }

  function ancsExpectedDetail() {
    var vendor = root.ancsVendorName();
    if (vendor !== "")
      return "Messages and contacts are connected. This " + vendor + " adapter does not support iPhone system notifications. Group texts will appear as separate messages from their sender.";
    return "Messages and contacts are connected. This Bluetooth adapter does not support iPhone system notifications. Group texts will appear as separate messages from their sender.";
  }

  function ancsUnavailableHint() {
    if (root.ancsLimited())
      return root.ancsExpectedDetail();
    return "FYI: If ANCS remains unavailable, BlueZ may be retaining stale Bluetooth state. Try running sudo systemctl restart bluetooth.service, then wait for BlueFerry to reconnect. This briefly disconnects all Bluetooth devices.";
  }

  color: root.theme.windowSurface
  radius: root.theme.panelRadius
  border.color: root.theme.divider

  ColumnLayout {
    anchors.fill: parent
    anchors.margins: root.theme.scaled(10)
    spacing: root.theme.scaled(6)

    ScrollView {
      id: iphoneScroll
      Layout.fillWidth: true
      Layout.fillHeight: true
      contentWidth: availableWidth

      ColumnLayout {
        width: Math.min(iphoneScroll.availableWidth, root.theme.scaled(620))
        x: Math.max(0, (iphoneScroll.availableWidth - width) / 2)
        spacing: root.theme.scaled(12)

        FerrySectionLabel {
          ferryTheme: root.theme
          text: "iPhone settings"
          topPadding: 0
        }
        FerryLabel {
          ferryTheme: root.theme
          text: root.setup.configured ? "Your iPhone" : "Connect an iPhone"
          font.pixelSize: root.theme.displaySize
          font.bold: true
        }
        Rectangle {
          Layout.fillWidth: true
          implicitHeight: 1
          color: root.theme.divider
        }
        FerryLabel {
          ferryTheme: root.theme
          text: root.setup.configurationError || root.setup.pairingStatus
          textFormat: Text.PlainText
          wrapMode: Text.Wrap
          Layout.fillWidth: true
          visible: text !== ""
        }
        FerryLabel {
          ferryTheme: root.theme
          objectName: "hardwareCompatibilityMessage"
          text: root.setup.compatibilityIssue
          textFormat: Text.PlainText
          wrapMode: Text.Wrap
          Layout.fillWidth: true
          visible: root.setup.compatibilityLoaded && !root.setup.pairingReady
        }
        FerryLabel {
          ferryTheme: root.theme
          text: onboarding.stage === "ready" ? "Bluetooth services and iPhone permissions have been verified." : onboarding.stage === "ready-without-ancs" ? (root.ancsLimited() ? root.ancsExpectedDetail() : "Messages and contacts have been verified. System notifications are unavailable, so group texts may appear as individual conversations.") : onboarding.stage === "iphone-settings" ? onboarding.mapConnectionRefused() ? "Cannot retrieve or send messages - are you connected to another computer? We will reconnect once your phone is free" : "Connected. Finish the remaining iPhone permissions below." : "Controller: " + (root.setup.adapterName || "checking…")
          wrapMode: Text.Wrap
          Layout.fillWidth: true
        }
        FerrySectionLabel {
          ferryTheme: root.theme
          text: "Pair an iPhone"
          visible: !root.setup.configured
        }
        FerryLabel {
          ferryTheme: root.theme
          text: "Open Bluetooth settings on your iPhone, scan, and select it below. Choose Pair and confirm that the codes match. Pairing can take about 15 seconds."
          wrapMode: Text.Wrap
          Layout.fillWidth: true
          visible: !root.setup.configured
        }
        FerryCheckBox {
          id: confirmBluetoothRestart
          ferryTheme: root.theme
          visible: !root.setup.configured && root.setup.notificationsSupported && !root.setup.bluezActive && !compatibilityMode.checked
          text: "I understand this briefly disconnects all Bluetooth devices"
        }
        FerryButton {
          ferryTheme: root.theme
          visible: !root.setup.configured && root.setup.notificationsSupported && !root.setup.bluezActive && !compatibilityMode.checked
          text: root.setup.activating ? "Activating…" : "Activate Bluetooth support"
          enabled: confirmBluetoothRestart.checked && !root.setup.activating
          onClicked: {
            root.setup.activateBluetooth();
            confirmBluetoothRestart.checked = false;
          }
        }
        FerryComboBox {
          id: adapterCombo
          ferryTheme: root.theme
          objectName: "adapterSelector"
          enabled: !root.setup.changingPhone && !root.setup.activating
          visible: !root.setup.configured && root.setup.adapters.length > 1
          Layout.fillWidth: true
          model: root.setup.adapters
          textRole: "label"
          currentIndex: root.setup.adapters.findIndex(adapter => adapter.name === root.setup.adapterName)
          onActivated: {
            var option = root.setup.adapters[currentIndex];
            if (option && option.name && option.name !== root.setup.adapterName)
              root.setup.loadCompatibility(option.name);
          }
        }
        FerryButton {
          ferryTheme: root.theme
          visible: !root.setup.configured
          objectName: "scanButton"
          text: root.setup.scanning ? "Cancel scan" : "Scan for iPhone"
          enabled: root.setup.compatibilityLoaded && !root.setup.changingPhone && !root.setup.activating
          onClicked: root.setup.scanning ? root.setup.cancelScan() : root.setup.loadDevices(true)
        }
        FerryComboBox {
          id: pairingDeviceCombo
          ferryTheme: root.theme
          objectName: "phoneSelector"
          visible: !root.setup.configured
          Layout.fillWidth: true
          model: root.setup.pairingDevices
          textRole: "label"
          currentIndex: root.setup.selectedDeviceIndex
          onActivated: root.setup.selectedDeviceIndex = currentIndex
        }
        FerryCheckBox {
          id: compatibilityMode
          ferryTheme: root.theme
          visible: !root.setup.configured
          text: "Compatibility pairing for iOS 18 or earlier"
          checked: root.setup.compatibilityLoaded && (!root.setup.notificationsSupported || root.setup.compatibilityModeOverride)
          enabled: root.setup.compatibilityLoaded && root.setup.notificationsSupported && !root.setup.pairing
          onClicked: root.setup.compatibilityModeOverride = checked
        }
        FerryCheckBox {
          id: explicitPairing
          objectName: "explicitPairingCheckBox"
          ferryTheme: root.theme
          visible: !root.setup.configured
          text: "Use explicit Bluetooth pairing"
          checked: root.setup.explicitPairing
          enabled: root.setup.compatibilityLoaded && !root.setup.pairing
          onClicked: root.setup.setExplicitPairing(checked)
        }
        FerryLabel {
          ferryTheme: root.theme
          visible: !root.setup.configured && compatibilityMode.checked && root.setup.pairingReady
          text: "Messages and contacts remain available. System notifications will be disabled; group texts may appear as individual conversations."
          wrapMode: Text.Wrap
          Layout.fillWidth: true
        }
        FerryButton {
          ferryTheme: root.theme
          visible: !root.setup.configured
          text: root.setup.pairing ? "Pairing…" : root.setup.selectedPairingDevice() && root.setup.selectedPairingDevice().paired ? "Use existing pairing" : "Pair selected iPhone"
          objectName: "pairPhoneButton"
          enabled: root.setup.canPair
          onClicked: {
            root.setup.requestPairing();
          }
        }
        FerryButton {
          ferryTheme: root.theme
          visible: root.setup.pairingIssueReport !== ""
          text: "Report Pairing Issue"
          onClicked: root.pairingIssueRequested()
        }
        FerryLabel {
          ferryTheme: root.theme
          text: root.setup.pairingConfirmationPurpose === "forget" ? "Confirm unpairing" : root.setup.pairingPasskey === "" ? "Pairing confirmation" : root.setup.pairingPasskey
          font.bold: true
          font.pixelSize: root.setup.pairingPasskey === "" ? root.theme.baseFontSize : root.theme.headingSize
          horizontalAlignment: Text.AlignHCenter
          Layout.fillWidth: true
          visible: root.setup.pairingConfirmationPending
        }
        RowLayout {
          Layout.fillWidth: true
          visible: root.setup.pairingConfirmationPending
          FerryButton {
            ferryTheme: root.theme
            text: root.setup.pairingConfirmationPurpose === "forget" ? "Cancel" : "Cancel Pairing"
            Layout.fillWidth: true
            onClicked: {
              root.setup.answerConfirmation(false);
            }
          }
          FerryButton {
            ferryTheme: root.theme
            text: root.setup.pairingConfirmationPurpose === "forget" ? "Unpair" : root.setup.pairingPasskey === "" ? "Approve Pairing" : "Codes Match"
            Layout.fillWidth: true
            highlighted: true
            onClicked: {
              root.setup.answerConfirmation(true);
            }
          }
        }
        RowLayout {
          visible: root.setup.targetSaved
          Layout.fillWidth: true
          FerryLabel {
            ferryTheme: root.theme
            Layout.fillWidth: true
            text: root.setup.configuredPairingDevice() ? root.setup.configuredPairingDevice().name : "iPhone"
            font.bold: true
          }
          FerryButton {
            ferryTheme: root.theme
            text: root.setup.forgetting ? "Removing…" : root.setup.bondStateKnown && !root.setup.targetBonded ? "Clear Saved Phone" : "Unpair"
            enabled: root.setup.configuredMac !== "" && !root.setup.changingPhone && !root.setup.activating
            onClicked: {
              root.setup.forgetPhone();
            }
          }
        }
        FerrySectionLabel {
          ferryTheme: root.theme
          text: "Finish Setup on the iPhone"
          visible: root.setup.configured && onboarding.pendingIphoneSetupTasks().length > 0
        }
        FerryLabel {
          ferryTheme: root.theme
          text: onboarding.pendingIphoneSetupText()
          wrapMode: Text.Wrap
          Layout.fillWidth: true
          visible: root.setup.configured && onboarding.pendingIphoneSetupTasks().length > 0
        }
        FerrySectionLabel {
          ferryTheme: root.theme
          text: "Connection health"
        }
        FerryInfoRow {
          ferryTheme: root.theme
          label: "Messages"
          value: onboarding.mapConnectionRefused() ? "Connection refused" : root.status.map ? "Connected" : "Unavailable"
          Layout.fillWidth: true
        }
        FerryLabel {
          ferryTheme: root.theme
          visible: onboarding.mapConnectionRefused()
          text: "iPhone is refusing message connections; is it connected to another computer?"
          textFormat: Text.PlainText
          color: root.theme.warning
          wrapMode: Text.Wrap
          Layout.fillWidth: true
        }
        FerryInfoRow {
          ferryTheme: root.theme
          label: "Contacts"
          value: root.status.pbap ? "Connected" : "Unavailable"
          Layout.fillWidth: true
        }
        FerryInfoRow {
          ferryTheme: root.theme
          label: "Notifications"
          value: root.status.ancs ? "Connected" : "Unavailable"
          Layout.fillWidth: true
        }
        FerryLabel {
          ferryTheme: root.theme
          visible: root.setup.configured && onboarding.notificationsSupported && root.status.map === true && root.status.pbap === true && !root.status.ancs
          text: root.ancsUnavailableHint()
          color: root.ancsLimited() ? root.theme.surfaceText : root.theme.warning
          wrapMode: Text.Wrap
          Layout.fillWidth: true
        }
        FerryLabel {
          ferryTheme: root.theme
          text: !root.setup.notificationsSupported ? "Bluetooth support: not required" : root.setup.bluezActive ? "Bluetooth support: active" : "Bluetooth support: restart required before pairing"
          color: root.setup.bluezActive || !root.setup.notificationsSupported ? root.theme.surfaceText : root.theme.warning
        }
        FerrySectionLabel {
          ferryTheme: root.theme
          text: "Desktop notifications"
        }
        FerryComboBox {
          id: notificationPolicyCombo
          ferryTheme: root.theme
          Layout.fillWidth: true
          model: ["All iPhone notifications", "Messages only", "None"]
          currentIndex: root.status.notification_policy === "all" ? 0 : root.status.notification_policy === "none" ? 2 : 1
          enabled: root.setup.configured && !root.busy.notifications
          onActivated: {
            var values = ["all", "messages", "none"];
            root.operationRequested("set_notification_policy", {
              policy: values[currentIndex]
            });
          }
        }
        FerryCheckBox {
          ferryTheme: root.theme
          text: "Only notify for contacts"
          checked: root.status.contacts_only_notifications
          enabled: root.setup.configured && root.status.notification_policy !== "none" && !root.busy.contactsOnly
          Accessible.description: "Unknown senders remain available in message history"
          onClicked: {
            root.operationRequested("set_contacts_only_notifications", {
              enabled: checked
            });
          }
        }
        FerrySectionLabel {
          ferryTheme: root.theme
          text: "Local data"
        }
        FerryComboBox {
          id: storagePolicyCombo
          objectName: "storagePolicySelector"
          ferryTheme: root.theme
          Layout.fillWidth: true
          model: ["Encrypted with desktop keyring", "Unencrypted local data", "Do not retain local data"]
          currentIndex: root.status.storage_policy === "plaintext" ? 1 : root.status.storage_policy === "none" ? 2 : 0
          enabled: root.setup.configured && !root.busy.storage
          onActivated: {
            var values = ["encrypted", "plaintext", "none"];
            if (values[currentIndex] !== root.status.storage_policy && !confirmStorageChange.checked) {
              currentIndex = Qt.binding(() => root.status.storage_policy === "plaintext" ? 1 : root.status.storage_policy === "none" ? 2 : 0);
              root.errorRequested("Confirm clearing local messages and contacts first");
              root.reloadRequested();
              return;
            }
            root.operationRequested("set_storage_policy", {
              policy: values[currentIndex]
            });
            confirmStorageChange.checked = false;
          }
        }
        FerryCheckBox {
          id: confirmStorageChange
          ferryTheme: root.theme
          text: "I understand that changing storage mode clears local messages and contacts"
        }
      }
    }

    RowLayout {
      Layout.fillWidth: true
      FerryButton {
        ferryTheme: root.theme
        text: "‹ MESSAGES"
        labelSize: root.theme.captionSize
        labelBold: true
        labelLetterSpacing: 1
        bare: true
        subtle: true
        leftPadding: root.theme.scaled(4)
        Accessible.name: "Back to messages"
        ToolTip.visible: hovered
        ToolTip.text: "Back to messages"
        onClicked: root.closeRequested()
      }
      Item {
        Layout.fillWidth: true
      }
    }
  }
}
