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
  // The bridge's features view, a choice being saved, the
  // diagnostics run and the recent-notification list (see shell.qml).
  property var features: ({})
  property var featurePending: ({})
  property var doctor: ({})
  property var notificationList: ({})
  property string contactsSyncNotice: ""
  // The list is private: shell.qml reads it only while this is set, and
  // hiding the page collapses it, so it is forgotten.
  property bool notificationsExpanded: false
  readonly property var reconnect: root.status.reconnect_view || ({})
  readonly property bool featuresAllowed: root.setup.configured
    && root.status.daemon === true && !root.busy.features
  readonly property var knownFeatures: ["show_notification_content", "mark_read_on_dismiss", "notification_history"]
  readonly property var theme: ferryTheme
  readonly property var connectionStatus: root.setup.pairing
    ? root.setup.pairingTransports : root.status
  readonly property bool showIphoneSetup: root.setup.compatibilityLoaded
    && onboarding.stage === "iphone-settings" && onboarding.pendingIphoneSetupTasks().length > 0
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
    backendStatus: root.connectionStatus
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
    // shell.qml passes the bridge's bluetooth_restart_command: the init
    // system's command, or "" when unknown. Older bridges omit it.
    var command = root.status.bluetooth_restart_command;
    if (typeof command !== "string")
      command = "sudo systemctl restart bluetooth.service";
    return "FYI: If ANCS remains unavailable, BlueZ may be retaining stale Bluetooth state. "
      + (command === "" ? "Try restarting the Bluetooth service" : "Try running " + command)
      + ", then wait for BlueFerry to reconnect. This briefly disconnects all Bluetooth devices.";
  }

  onVisibleChanged: if (!visible) root.notificationsExpanded = false

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
        // A switch that needs a restart: features_view's notice.
        RowLayout {
          objectName: "restartNotice"
          Layout.fillWidth: true
          visible: root.features.restart_pending === true
          FerryLabel {
            ferryTheme: root.theme
            Layout.fillWidth: true
            wrapMode: Text.Wrap
            color: root.theme.warning
            text: String(root.features.restart_notice || "")
          }
          FerryButton {
            objectName: "restartServiceButton"
            ferryTheme: root.theme
            text: root.busy.restart ? "Restarting…" : "Restart Service"
            enabled: !root.busy.restart
            onClicked: root.operationRequested("restart_backend", {})
          }
        }
        FerryCategoryHeading {
          objectName: "phoneCategory"
          ferryTheme: root.theme
          text: "Phone"
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
          objectName: "iphoneSetupHeading"
          text: "Finish Setup on the iPhone"
          visible: root.showIphoneSetup
        }
        FerryLabel {
          ferryTheme: root.theme
          objectName: "iphoneSetupInstructions"
          text: onboarding.pendingIphoneSetupText()
          wrapMode: Text.Wrap
          Layout.fillWidth: true
          visible: root.showIphoneSetup
        }
        FerrySectionLabel {
          ferryTheme: root.theme
          text: "Connection health"
        }
        FerryInfoRow {
          ferryTheme: root.theme
          objectName: "messagesConnection"
          label: "Messages"
          value: onboarding.mapConnectionRefused() ? "Connection refused" : root.connectionStatus.map ? "Connected" : "Unavailable"
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
          objectName: "contactsConnection"
          label: "Contacts"
          value: root.connectionStatus.pbap ? "Connected" : "Unavailable"
          Layout.fillWidth: true
        }
        FerryInfoRow {
          ferryTheme: root.theme
          objectName: "notificationsConnection"
          label: "Notifications"
          value: root.connectionStatus.ancs ? "Connected" : "Unavailable"
          Layout.fillWidth: true
        }
        FerryLabel {
          ferryTheme: root.theme
          visible: root.setup.configured && onboarding.notificationsSupported && root.connectionStatus.map === true && root.connectionStatus.pbap === true && !root.connectionStatus.ancs
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
        // Manual reconnect, as in the other clients: the automatic backoff
        // can wait up to ten minutes. Hint and outcome are the shared
        // reconnect_view texts the bridge adds to the status.
        RowLayout {
          objectName: "reconnectRow"
          Layout.fillWidth: true
          visible: root.reconnect.available === true
          FerryLabel {
            objectName: "reconnectHint"
            ferryTheme: root.theme
            Layout.fillWidth: true
            wrapMode: Text.Wrap
            color: root.reconnect.attention === true ? root.theme.warning : root.theme.windowText
            text: String(root.reconnect.hint || "")
          }
          FerryButton {
            objectName: "reconnectButton"
            ferryTheme: root.theme
            visible: root.reconnect.offered === true
            text: root.busy.reconnect ? "Reconnecting…" : "Reconnect"
            enabled: root.setup.configured && !root.busy.reconnect
            onClicked: root.operationRequested("reconnect_phone", {})
          }
        }
        FerryCheckBox {
          objectName: "autoProfileResetCheckBox"
          ferryTheme: root.theme
          visible: root.status.profile_reset_auto !== undefined
          text: "Reset a stuck iPhone connection automatically"
          checked: root.status.profile_reset_auto === true
          enabled: root.setup.configured && !root.busy.autoProfileReset
          Accessible.description: "Briefly disconnects the iPhone when call audio stays stuck, at most every ten minutes. Reconnect still resets it on request."
          onClicked: root.operationRequested("set_auto_profile_reset", {enabled: checked})
        }
        FerrySectionLabel {
          ferryTheme: root.theme
          text: "Diagnostics"
        }
        FerryLabel {
          ferryTheme: root.theme
          Layout.fillWidth: true
          wrapMode: Text.Wrap
          text: "Checks the Bluetooth controller, the BlueZ OBEX service, the configured iPhone and the notification link, like the command blueferry doctor."
        }
        FerryButton {
          objectName: "runDoctorButton"
          ferryTheme: root.theme
          text: root.doctor.running === true ? "Running Diagnostics…" : "Run Diagnostics"
          enabled: root.doctor.running !== true
          onClicked: root.operationRequested("doctor", {})
        }
        // level and summary come from doctor_report.view(), shared with Qt
        // and GTK. The report is local diagnostics, shown as plain text.
        FerryLabel {
          objectName: "doctorSummary"
          ferryTheme: root.theme
          Layout.fillWidth: true
          visible: root.doctor.ran === true
          wrapMode: Text.Wrap
          font.bold: true
          color: root.doctor.level === "ok" ? root.theme.windowText : root.theme.warning
          text: String(root.doctor.summary || "")
        }
        TextEdit {
          objectName: "doctorReport"
          Layout.fillWidth: true
          visible: root.doctor.ran === true
          readOnly: true
          selectByMouse: true
          textFormat: TextEdit.PlainText
          wrapMode: TextEdit.Wrap
          color: root.theme.windowText
          selectionColor: root.theme.accent
          selectedTextColor: root.theme.highlightedText
          font.family: root.theme.fontFamily
          font.pixelSize: root.theme.captionSize
          text: String(root.doctor.text || "")
        }
        FerrySectionLabel {
          ferryTheme: root.theme
          text: "Contacts"
        }
        RowLayout {
          Layout.fillWidth: true
          FerryButton {
            objectName: "syncContactsButton"
            ferryTheme: root.theme
            text: root.busy.syncContacts ? "Syncing…" : "Sync Contacts"
            enabled: root.setup.configured && root.status.daemon === true && !root.busy.syncContacts
            onClicked: root.operationRequested("sync_contacts", {})
          }
          FerryLabel {
            objectName: "syncContactsNotice"
            ferryTheme: root.theme
            Layout.fillWidth: true
            wrapMode: Text.Wrap
            visible: text !== ""
            text: root.contactsSyncNotice
          }
        }
        // Only daemons that report contact_photos offer the setting.
        FerrySectionLabel {
          ferryTheme: root.theme
          text: "Contact photos"
          visible: root.status.saved_opt_ins === true && root.status.contact_photos !== undefined
        }
        FerryCheckBox {
          objectName: "contactPhotosCheckBox"
          ferryTheme: root.theme
          text: "Show contact photos from the iPhone"
          visible: root.status.saved_opt_ins === true && root.status.contact_photos !== undefined
          checked: root.status.contact_photos === true
          enabled: root.setup.configured && !root.busy.photos
          Accessible.description: "Photos are kept with your contacts and arrive with the next contact sync. Turning this off erases them."
          onClicked: {
            root.operationRequested("set_contact_photos", {
              enabled: checked
            });
          }
        }
        FerryCategoryHeading {
          objectName: "notificationsCategory"
          ferryTheme: root.theme
          text: "Notifications"
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
          text: "Content and actions"
        }
        // Why the switches below fall back to their local.env variables. Only
        // while the service is away: an older daemon without GetFeatures is
        // explained by each row's note, as in the Qt and GTK clients.
        FerryLabel {
          objectName: "featuresError"
          ferryTheme: root.theme
          Layout.fillWidth: true
          visible: text !== "" && root.status.daemon !== true
          wrapMode: Text.Wrap
          color: root.theme.warning
          text: root.features.error ? "Could not read the service's switches: " + String(root.features.error) : ""
        }
        FerryFeatureSwitch {
          ferryTheme: root.theme
          features: root.features
          pending: root.featurePending
          allowed: root.featuresAllowed
          feature: "show_notification_content"
          text: "Show message content in popups"
          description: "Off: popups name the sender only."
          onToggledTo: (feature, enabled) => root.operationRequested("set_feature", {name: feature, enabled: enabled})
        }
        // Opt-in; only daemons that report the preference support it.
        FerryCheckBox {
          objectName: "ancsActionsCheckBox"
          ferryTheme: root.theme
          visible: root.status.ancs_actions_preference !== undefined
          text: "Show iPhone action buttons (Accept, Decline, Clear…)"
          checked: root.status.ancs_actions_preference === true
          // A saved "on" can always be switched off; switching on needs All
          // iPhone Notifications with content shown.
          enabled: root.setup.configured && !root.busy.ancsActions && (checked || (root.status.notification_policy === "all" && root.status.notification_content_shown !== false))
          Accessible.description: root.status.notification_content_shown === false ? "Unavailable while notification content is hidden." : "Clicking a button runs that action on the iPhone, for example answering or declining a call. Applies to All iPhone notifications."
          onClicked: {
            root.operationRequested("set_ancs_notification_actions", {
              enabled: checked
            });
          }
        }
        // Opt-in; only daemons that report mirror_iphone_removals support it.
        FerryCheckBox {
          objectName: "mirrorRemovalsCheckBox"
          ferryTheme: root.theme
          visible: typeof root.status.mirror_iphone_removals === "boolean"
          text: "Sync notifications with iPhone"
          checked: root.status.mirror_iphone_removals === true
          enabled: root.setup.configured && !root.busy.mirrorRemovals
          Accessible.description: mirrorRemovalsNote.text
          onClicked: root.operationRequested("set_mirror_notification_removals", {enabled: checked})
        }
        FerryLabel {
          id: mirrorRemovalsNote
          ferryTheme: root.theme
          visible: typeof root.status.mirror_iphone_removals === "boolean"
          Layout.fillWidth: true
          wrapMode: Text.Wrap
          text: "A notification removed or dismissed on the iPhone also closes its popup here. Applies to All iPhone notifications."
        }
        FerryFeatureSwitch {
          ferryTheme: root.theme
          features: root.features
          pending: root.featurePending
          allowed: root.featuresAllowed
          feature: "mark_read_on_dismiss"
          text: "Mark read when a popup is dismissed"
          onToggledTo: (feature, enabled) => root.operationRequested("set_feature", {name: feature, enabled: enabled})
        }
        FerrySectionLabel {
          ferryTheme: root.theme
          text: "History"
        }
        FerryFeatureSwitch {
          ferryTheme: root.theme
          features: root.features
          pending: root.featurePending
          allowed: root.featuresAllowed
          feature: "notification_history"
          text: "Keep recent iPhone notifications"
          description: "Lists other apps' notifications under Recent notifications below (memory only)."
          onToggledTo: (feature, enabled) => root.operationRequested("set_feature", {name: feature, enabled: enabled})
        }
        // Switches a newer service reports that this shell has no wording for.
        Repeater {
          objectName: "otherFeatureSwitches"
          model: root.features.switches && root.features.switches.length !== undefined
            ? Array.prototype.slice.call(root.features.switches) : []
          delegate: FerryFeatureSwitch {
            required property var modelData
            readonly property var row: modelData || ({})
            ferryTheme: root.theme
            features: root.features
            pending: root.featurePending
            allowed: root.featuresAllowed
            feature: String(row.name || "")
            visible: root.knownFeatures.indexOf(feature) < 0
            onToggledTo: (feature, enabled) => root.operationRequested("set_feature", {name: feature, enabled: enabled})
          }
        }
        // Private: read only while shown, forgotten when hidden (the page
        // collapses it whenever it is hidden), as in the other clients.
        FerrySectionLabel {
          ferryTheme: root.theme
          text: "Recent notifications"
        }
        FerryButton {
          objectName: "notificationListButton"
          ferryTheme: root.theme
          text: root.notificationsExpanded ? "Hide Recent Notifications" : "Show Recent Notifications"
          enabled: root.setup.configured
          onClicked: root.notificationsExpanded = !root.notificationsExpanded
        }
        QuickshellNotificationList {
          objectName: "notificationList"
          ferryTheme: root.theme
          Layout.fillWidth: true
          visible: root.notificationsExpanded
          view: root.notificationList
        }
        FerryCategoryHeading {
          objectName: "callsCategory"
          ferryTheme: root.theme
          text: "Calls & Audio"
        }
        // Shown only when the status carries calls_enabled, as in the Qt client.
        FerrySectionLabel {
          ferryTheme: root.theme
          visible: root.status.calls_enabled !== undefined
          text: "Phone calls"
        }
        FerryCheckBox {
          objectName: "callsEnabledCheckBox"
          ferryTheme: root.theme
          visible: root.status.calls_enabled !== undefined
          text: "Enable phone calls through this computer"
          checked: root.status.calls_enabled === true
          enabled: root.setup.configured && !root.busy.calls
          Accessible.description: "Experimental. Needs oFono and extra setup."
          onClicked: {
            root.operationRequested("set_calls_enabled", {
              enabled: checked
            });
          }
        }
        FerryLabel {
          ferryTheme: root.theme
          visible: root.status.calls_enabled !== undefined
          Layout.fillWidth: true
          wrapMode: Text.Wrap
          text: "Experimental. While this is on, the iPhone's hands-free link stays connected to this computer, so calls can ring and be answered here and their audio plays here. Music stays on the iPhone. Working calls also need oFono set as the hands-free backend and BlueZ's own HFP plugin disabled; see \"Phone calls\" in the BlueFerry documentation. Make emergency calls on the iPhone itself."
        }
        // Only daemons that report the call-history keys support the settings.
        FerrySectionLabel {
          ferryTheme: root.theme
          visible: root.status.call_history_enabled !== undefined
          text: "Call history"
        }
        FerryCheckBox {
          objectName: "callHistoryCheckBox"
          ferryTheme: root.theme
          visible: root.status.call_history_enabled !== undefined
          text: "Keep the iPhone's recent calls"
          checked: root.status.call_history_enabled === true
          enabled: root.setup.configured && !root.busy.callHistory
          Accessible.description: callHistoryNote.text
          onClicked: {
            root.operationRequested("set_call_history", {
              enabled: checked,
              missed_call_notifications: missedCallPopupsCheckBox.checked
            });
          }
        }
        FerryCheckBox {
          id: missedCallPopupsCheckBox
          objectName: "missedCallPopupsCheckBox"
          ferryTheme: root.theme
          visible: root.status.call_history_enabled !== undefined
          text: "Notify me about missed calls"
          checked: root.status.missed_call_notifications !== false
          // Only offered while call history is on, as in the Qt client.
          enabled: root.setup.configured && !root.busy.callHistory
            && root.status.call_history_enabled === true
          onClicked: {
            root.operationRequested("set_call_history", {
              enabled: true,
              missed_call_notifications: checked
            });
          }
        }
        FerryLabel {
          id: callHistoryNote
          objectName: "callHistoryNote"
          ferryTheme: root.theme
          visible: root.status.call_history_enabled !== undefined
          Layout.fillWidth: true
          wrapMode: Text.Wrap
          text: "Keeps the iPhone's recent calls (who called and when) under your local storage setting and can notify you about missed calls. It uses the iPhone's Sync Contacts permission and never places, answers, or listens to calls. Turning it off erases the retained calls."
        }
        // BackendStatus always fills phone_battery_warning; only daemons
        // that offer the warning report its threshold.
        FerrySectionLabel {
          ferryTheme: root.theme
          visible: root.status.phone_battery_warning_percent !== undefined
          text: "Battery"
        }
        FerryCheckBox {
          objectName: "phoneBatteryWarningCheckBox"
          ferryTheme: root.theme
          visible: root.status.phone_battery_warning_percent !== undefined
          text: "Warn when the iPhone's battery runs low"
          checked: root.status.phone_battery_warning === true
          enabled: root.setup.configured && !root.busy.batteryWarning
          Accessible.description: batteryWarningNote.text
          onClicked: root.operationRequested("set_phone_battery_warning", {enabled: checked})
        }
        FerryLabel {
          id: batteryWarningNote
          objectName: "phoneBatteryWarningNote"
          ferryTheme: root.theme
          visible: root.status.phone_battery_warning_percent !== undefined
          Layout.fillWidth: true
          wrapMode: Text.Wrap
          text: "One desktop notification per discharge, at " + (Number(root.status.phone_battery_warning_percent) || 20) + " % or less."
        }
        // Only daemons that report the audio-route keys can switch the sound.
        // An action, not a saved preference: it follows the reported route.
        FerrySectionLabel {
          ferryTheme: root.theme
          visible: root.status.phone_audio_route !== undefined
          text: "iPhone sound"
        }
        FerryCheckBox {
          objectName: "phoneAudioCheckBox"
          ferryTheme: root.theme
          readonly property var view: root.status.phone_audio_view || ({})
          visible: root.status.phone_audio_route !== undefined
          text: "Play the iPhone's sound on this computer"
          checked: root.status.phone_audio_route === "pc"
          enabled: root.setup.configured && !root.busy.phoneAudio && view.available === true
          Accessible.description: phoneAudioHint.text
          onClicked: root.operationRequested("set_phone_audio_route", {route: checked ? "pc" : "phone"})
        }
        FerryLabel {
          id: phoneAudioHint
          objectName: "phoneAudioHint"
          ferryTheme: root.theme
          visible: root.status.phone_audio_route !== undefined && text !== ""
          Layout.fillWidth: true
          wrapMode: Text.Wrap
          text: (root.status.phone_audio_view || {}).hint || ""
        }

        // Only daemons that report the media-control keys support the settings.
        FerrySectionLabel {
          ferryTheme: root.theme
          visible: root.status.media_control_enabled !== undefined
          text: "Media control"
        }
        FerryCheckBox {
          objectName: "mediaControlCheckBox"
          ferryTheme: root.theme
          visible: root.status.media_control_enabled !== undefined
          text: "Show and control what the iPhone is playing"
          checked: root.status.media_control_enabled === true
          enabled: root.setup.configured && !root.busy.mediaControl
          Accessible.description: "Uses the Bluetooth LE link that also carries notifications."
          onClicked: root.operationRequested("set_media_control", {enabled: checked})
        }
        FerryCheckBox {
          objectName: "mprisPlayerCheckBox"
          ferryTheme: root.theme
          visible: root.status.media_mpris_enabled !== undefined
          text: "Also show it in the desktop media controls (MPRIS)"
          checked: root.status.media_mpris_enabled === true
          // The player needs media control, as in the Qt client.
          enabled: root.setup.configured && !root.busy.mprisPlayer
            && !root.busy.mediaControl && root.status.media_control_enabled === true
          Accessible.description: mprisPlayerWarning.text
          onClicked: root.operationRequested("set_mpris_player", {enabled: checked})
        }
        FerryLabel {
          id: mprisPlayerWarning
          objectName: "mprisPlayerWarning"
          ferryTheme: root.theme
          visible: root.status.media_mpris_enabled !== undefined
          Layout.fillWidth: true
          wrapMode: Text.Wrap
          text: "Like any desktop music player, every application in your session can then read the title, artist and album."
        }
        FerryCategoryHeading {
          objectName: "securityCategory"
          ferryTheme: root.theme
          text: "Security"
        }
        // Only daemons that report otp_autocopy offer the setting.
        FerrySectionLabel {
          ferryTheme: root.theme
          text: "One-time codes"
          visible: root.status.saved_opt_ins === true && root.status.otp_autocopy !== undefined
        }
        FerryCheckBox {
          objectName: "otpAutocopyCheckBox"
          ferryTheme: root.theme
          text: "Copy one-time codes to the clipboard"
          visible: root.status.saved_opt_ins === true && root.status.otp_autocopy !== undefined
          checked: root.status.otp_autocopy === true
          enabled: root.setup.configured && !root.busy.otp
          Accessible.description: "Only new messages from senders not in your contacts, never group chats. Turning this off clears a copied code still on the clipboard."
          onClicked: {
            root.operationRequested("set_otp_autocopy", {
              enabled: checked
            });
          }
        }
        // Only daemons that report the away-lock keys support the setting.
        FerrySectionLabel {
          ferryTheme: root.theme
          visible: root.status.proximity_lock !== undefined
          text: "Away lock"
        }
        FerryCheckBox {
          objectName: "proximityLockCheckBox"
          ferryTheme: root.theme
          visible: root.status.proximity_lock !== undefined
          text: "Lock the desktop when my iPhone goes away"
          checked: root.status.proximity_lock_enabled === true
          enabled: root.setup.configured && !root.busy.proximityLock
          Accessible.description: "A convenience, not a security feature. BlueFerry never unlocks the desktop."
          onClicked: {
            // The checkbox only opts in or out; the saved grace period is kept.
            root.operationRequested("set_proximity_lock", {
              enabled: checked,
              grace_seconds: root.status.proximity_lock_grace_sec || 60
            });
          }
        }
        FerryLabel {
          ferryTheme: root.theme
          visible: root.status.proximity_lock !== undefined
          Layout.fillWidth: true
          wrapMode: Text.Wrap
          text: "Locks after " + (root.status.proximity_lock_grace_sec || 60) + " seconds away. A convenience, not a security feature: Bluetooth presence can be spoofed, and BlueFerry never unlocks the desktop."
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
        FerryCategoryHeading {
          objectName: "aboutCategory"
          ferryTheme: root.theme
          text: "About"
        }
        FerrySectionLabel {
          ferryTheme: root.theme
          text: "BlueFerry"
        }
        FerryInfoRow {
          objectName: "serviceState"
          ferryTheme: root.theme
          label: "Background service"
          value: root.status.daemon === true ? "Running" : "Unavailable"
          Layout.fillWidth: true
        }
        FerryInfoRow {
          ferryTheme: root.theme
          visible: root.status.daemon === true && typeof root.status.backend_release === "string"
          label: "Service version"
          value: String(root.status.backend_release || "")
          Layout.fillWidth: true
        }
        FerryButton {
          objectName: "aboutRestartButton"
          ferryTheme: root.theme
          text: root.busy.restart ? "Restarting…" : "Restart Service"
          enabled: !root.busy.restart
          onClicked: root.operationRequested("restart_backend", {})
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
