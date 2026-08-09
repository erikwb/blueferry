pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io

ShellRoot {
  id: root
  property var threads: []
  property string selectedThreadKey: ""
  property string confirmedGroupSignature: ""
  property string errorText: ""
  property bool phoneSettingsVisible: false
  property var pairingDevices: []
  property string pairingStatus: "Step 1: scan for the iPhone. Scanning does not pair it."
  property bool bluezActive: false
  property bool hardwareSupported: false
  property bool notificationsSupported: false
  property bool pairingReady: false
  property bool configured: false
  property string configuredMac: ""
  property string adapterName: ""
  property string onboardingStage: "checking"
  property var backendStatus: ({})
  property string notificationPolicy: "messages"
  property string storagePolicy: "encrypted"
  property string storageState: "locked"
  property string storageDetail: ""
  property bool storageUnlockAttempted: false
  property string statusErrorText: ""

  Theme { id: theme }

  Connections {
    target: Quickshell
    function onLastWindowClosed() { Qt.quit() }
  }

  function reload() {
    if (!configured) return
    if (!threadsProcess.running) threadsProcess.running = true
    if (!statusProcess.running) statusProcess.running = true
  }

  function maybeUnlockStorage() {
    if (configured && storagePolicy === "encrypted" && storageState !== "ready"
        && !storageUnlockAttempted && !storageUnlockProcess.running) {
      storageUnlockAttempted = true
      storageUnlockProcess.running = true
    }
  }

  function selectedThread() {
    for (var index = 0; index < threads.length; ++index) {
      if (threads[index].key === selectedThreadKey) return threads[index]
    }
    return null
  }

  function groupSignature(thread) {
    if (!thread || !thread.is_group) return ""
    return thread.key + "\n" + JSON.stringify(thread.recipients || [])
  }

  function pendingIphoneSetupTasks() {
    var verified = backendStatus.verified_iphone_setup || []
    var tasks = []
    if (verified.indexOf("message-notifications") < 0)
      tasks.push("Enable Show Message Notifications")
    if (verified.indexOf("contacts") < 0)
      tasks.push("Enable Sync Contacts")
    if (notificationsSupported && verified.indexOf("notification-access") < 0)
      tasks.push("Allow Notification Access when prompted")
    return tasks
  }

  function pendingIphoneSetupText() {
    return "On the iPhone open Settings → Bluetooth, tap ⓘ next to this computer, then finish:\n• "
      + pendingIphoneSetupTasks().join("\n• ")
  }

  function updateOnboarding() {
    var device = selectedPairingDevice()
    if (!hardwareSupported) onboardingStage = "incompatible"
    else if (notificationsSupported && !bluezActive) onboardingStage = "activate-bluetooth"
    else if (!configured) onboardingStage = "select-device"
    else if (backendStatus.map && backendStatus.pbap) {
      if (pendingIphoneSetupTasks().length > 0) onboardingStage = "iphone-settings"
      else if (!notificationsSupported) onboardingStage = "ready-without-ancs"
      else onboardingStage = "ready"
    }
    else if (backendStatus.daemon) onboardingStage = "iphone-settings"
    else onboardingStage = "starting"
  }

  function loadPairingDevices(scan) {
    if (deviceProcess.running) return
    deviceProcess.command = scan
      ? ["/usr/bin/blueferry", "pairing-devices-json", "--scan-seconds", "8"]
      : ["/usr/bin/blueferry", "pairing-devices-json"]
    pairingStatus = scan ? "Scanning for Bluetooth devices…" : pairingStatus
    deviceProcess.running = true
  }

  function selectedPairingDevice() {
    var index = pairingDeviceCombo.currentIndex
    return index >= 0 && index < pairingDevices.length ? pairingDevices[index] : null
  }

  function configuredPairingDevice() {
    for (var index = 0; index < pairingDevices.length; ++index) {
      if (pairingDevices[index].mac === configuredMac) return pairingDevices[index]
    }
    return null
  }

  function markStatusUnavailable(message) {
    backendStatus = ({})
    statusErrorText = message
    updateOnboarding()
  }

  function markCompatibilityUnavailable(message) {
    hardwareSupported = false
    notificationsSupported = false
    pairingReady = false
    bluezActive = false
    adapterName = ""
    pairingStatus = message
    updateOnboarding()
  }

  Process {
    id: statusProcess
    command: ["/usr/bin/blueferry", "status-json"]
    stdout: StdioCollector {
      onStreamFinished: {
        try {
          var parsed = JSON.parse(text)
          root.backendStatus = parsed
          var policy = parsed.notification_policy || "messages"
          root.notificationPolicy = ["all", "messages", "none"].indexOf(policy) >= 0
            ? policy : "messages"
          var storagePolicy = parsed.storage_policy || "encrypted"
          root.storagePolicy = ["encrypted", "plaintext", "none"].indexOf(storagePolicy) >= 0
            ? storagePolicy : "encrypted"
          root.storageState = parsed.storage_state || "locked"
          root.storageDetail = parsed.storage_detail || "Storage status unavailable"
          root.statusErrorText = ""
          root.updateOnboarding()
          root.maybeUnlockStorage()
        } catch (error) {
          root.markStatusUnavailable("BlueFerry backend returned invalid status data")
        }
      }
    }
    // qmllint disable signal-handler-parameters
    onExited: function(code) {
      if (code !== 0) root.markStatusUnavailable("BlueFerry backend is unavailable")
    }
  }

  Process {
    id: compatibilityProcess
    command: ["/usr/bin/blueferry", "pairing-compatibility-json"]
    stdout: StdioCollector {
      onStreamFinished: {
        try {
          var parsed = JSON.parse(text)
          root.hardwareSupported = parsed.hardware_supported === true
          root.notificationsSupported = parsed.notifications_supported === true
          root.pairingReady = parsed.pairing_ready === true
          root.bluezActive = parsed.bearer_api_active === true
          root.adapterName = parsed.adapter || ""
          if (!root.hardwareSupported) root.pairingStatus = parsed.issue || "Bluetooth controller is incompatible."
          root.updateOnboarding()
          root.loadPairingDevices(false)
        } catch (error) {
          root.markCompatibilityUnavailable("Bluetooth compatibility check returned invalid data.")
        }
      }
    }
    // qmllint disable signal-handler-parameters
    onExited: function(code) {
      if (code !== 0)
        root.markCompatibilityUnavailable("Bluetooth compatibility check failed.")
    }
  }

  Process {
    id: configurationProcess
    command: ["/usr/bin/blueferry", "pairing-configuration-json"]
    stdout: StdioCollector {
      onStreamFinished: {
        try {
          var parsed = JSON.parse(text)
          root.configured = parsed.configured === true
          root.configuredMac = root.configured ? (parsed.mac || "") : ""
          if (!root.configured) root.phoneSettingsVisible = true
          else root.reload()
          root.updateOnboarding()
        } catch (error) { root.phoneSettingsVisible = true }
      }
    }
  }

  Process {
    id: threadsProcess
    command: ["/usr/bin/blueferry", "threads-json", "--limit", "200"]
    stdout: StdioCollector {
      onStreamFinished: {
        try {
          var parsed = JSON.parse(text)
          root.threads = Array.isArray(parsed) ? parsed : []
          if (root.selectedThreadKey !== "" && root.selectedThread() === null) {
            root.selectedThreadKey = ""
            root.confirmedGroupSignature = ""
          }
          root.errorText = ""
        } catch (error) { root.errorText = "Backend response was invalid" }
      }
    }
    // Quickshell's qmltypes omit the QProcess namespace used by this signal.
    // qmllint disable signal-handler-parameters
    onExited: function(code) {
      if (code !== 0) root.errorText = "BlueFerry daemon is unavailable"
    }
  }

  Process {
    id: sendProcess
    stdout: StdioCollector { }
    stderr: StdioCollector {
      onStreamFinished: if (text.trim() !== "") root.errorText = text.trim()
    }
    // Quickshell's qmltypes omit the QProcess namespace used by this signal.
    // qmllint disable signal-handler-parameters
    onExited: function(code) {
      if (code === 0) {
        composer.text = ""
        root.reload()
      }
    }
  }

  Process {
    id: notificationPolicyProcess
    stdout: StdioCollector {
      onStreamFinished: {
        try {
          var parsed = JSON.parse(text)
          if (parsed.ok) root.notificationPolicy = parsed.policy
          else root.errorText = parsed.error || "Could not save notification preference"
        } catch (error) { root.errorText = "Notification preference response was invalid" }
      }
    }
    // qmllint disable signal-handler-parameters
    onExited: function(code) {
      if (code === 0) root.reload()
    }
  }

  Process {
    id: storagePolicyProcess
    stdout: StdioCollector {
      onStreamFinished: {
        try {
          var parsed = JSON.parse(text)
          root.storagePolicy = parsed.storage_policy || root.storagePolicy
          root.storageState = parsed.storage_state || root.storageState
          root.storageDetail = parsed.storage_detail || root.storageDetail
        } catch (error) { root.errorText = "Storage response was invalid" }
      }
    }
    stderr: StdioCollector {
      onStreamFinished: if (text.trim() !== "") root.errorText = text.trim()
    }
    // qmllint disable signal-handler-parameters
    onExited: function(code) { if (code === 0) root.reload() }
  }

  Process {
    id: storageUnlockProcess
    command: ["/usr/bin/blueferry", "storage-unlock"]
    stdout: StdioCollector {
      onStreamFinished: {
        try {
          var parsed = JSON.parse(text)
          root.storageState = parsed.storage_state || root.storageState
          root.storageDetail = parsed.storage_detail || root.storageDetail
        } catch (error) { root.errorText = "Keyring response was invalid" }
      }
    }
    stderr: StdioCollector {
      onStreamFinished: if (text.trim() !== "") root.errorText = text.trim()
    }
    // qmllint disable signal-handler-parameters
    onExited: function(code) { if (code === 0) root.reload() }
  }

  Process {
    id: deviceProcess
    stdout: StdioCollector {
      onStreamFinished: {
        try {
          var parsed = JSON.parse(text)
          if (parsed.error) {
            root.pairingStatus = parsed.error
            return
          }
          var matching = parsed.filter(function(device) {
            return !root.adapterName || device.adapter_path.endsWith("/" + root.adapterName)
          })
          root.pairingDevices = matching.map(function(device) {
            device.label = device.name + " — " + device.mac
              + (device.paired ? " (paired)" : "")
            return device
          })
          root.pairingStatus = matching.length
            ? root.configured
              ? "Linux pairing is complete. Check the required iPhone settings below."
              : "Step 2: select the iPhone, then choose Pair Selected iPhone."
            : "No devices found. Unlock the iPhone, keep Bluetooth settings open, and scan again."
          root.updateOnboarding()
        } catch (error) { root.pairingStatus = "Bluetooth scan returned invalid data." }
      }
    }
  }

  Process {
    id: bluezStatusProcess
    command: ["/usr/bin/blueferry", "pairing-bluez-status-json"]
    stdout: StdioCollector {
      onStreamFinished: {
        try {
          var parsed = JSON.parse(text)
          root.bluezActive = parsed.active === true
        } catch (error) { root.bluezActive = false }
      }
    }
  }

  Process {
    id: bluezActivateProcess
    command: ["/usr/bin/blueferry", "pairing-activate-bluez"]
    stdout: StdioCollector {
      onStreamFinished: {
        try {
          var parsed = JSON.parse(text)
          if (parsed.ok) {
            root.bluezActive = true
            root.pairingStatus = "Bluetooth support activated. Scan for the iPhone."
            root.loadPairingDevices(true)
            compatibilityProcess.running = true
          } else {
            root.pairingStatus = parsed.error || "Bluetooth activation failed."
          }
        } catch (error) { root.pairingStatus = "Bluetooth activation returned invalid data." }
      }
    }
  }

  Process {
    id: pairProcess
    stdout: StdioCollector {
      onStreamFinished: {
        try {
          var parsed = JSON.parse(text)
          if (!parsed.ok) {
            root.pairingStatus = parsed.error || "Pairing failed; it is safe to retry."
            return
          }
          root.pairingStatus = parsed.ancs_ready || !root.notificationsSupported
            ? "Linux pairing complete. On the iPhone enable Show Message Notifications and Sync Contacts."
            : "Pairing is complete. Notification access is still settling; keep the iPhone Bluetooth settings open."
          root.configured = true
          root.configuredMac = parsed.device ? (parsed.device.mac || "") : ""
          root.loadPairingDevices(false)
          root.reload()
          root.updateOnboarding()
        } catch (error) { root.pairingStatus = "Pairing returned invalid data." }
      }
    }
  }

  Process {
    id: forgetProcess
    stdout: StdioCollector {
      onStreamFinished: {
        try {
          var parsed = JSON.parse(text)
          root.pairingStatus = parsed.ok
            ? "Local bond removed. Also forget this computer on the iPhone, then scan again."
            : (parsed.error || "Could not remove the bond.")
          if (parsed.ok) {
            root.configured = false
            root.configuredMac = ""
            root.backendStatus = ({})
            root.pairingDevices = []
            root.updateOnboarding()
            root.loadPairingDevices(false)
          }
        } catch (error) { root.pairingStatus = "Forget operation returned invalid data." }
      }
    }
  }

  Timer {
    interval: 3000
    repeat: true
    running: true
    triggeredOnStart: true
    onTriggered: root.reload()
  }

  Component.onCompleted: {
    compatibilityProcess.running = true
    configurationProcess.running = true
    loadPairingDevices(false)
  }

  FloatingWindow {
    id: window
    title: "BlueFerry"
    implicitWidth: 760
    implicitHeight: 620
    color: theme.windowSurface

    Pane {
      id: applicationSurface
      anchors.fill: parent
      padding: 0
      font.family: theme.fontFamily
      font.pixelSize: theme.baseFontSize
      palette.window: theme.windowSurface
      palette.windowText: theme.windowText
      palette.base: theme.windowSurface
      palette.alternateBase: theme.alternate
      palette.text: theme.windowText
      palette.button: theme.control
      palette.buttonText: theme.windowText
      palette.highlight: theme.accent
      palette.highlightedText: theme.highlightedText
      palette.placeholderText: theme.muted
      palette.mid: theme.surfaceBorder
      palette.toolTipBase: theme.surface
      palette.toolTipText: theme.surfaceText

      background: Rectangle {
        radius: theme.cornerRadius
        color: theme.windowSurface
        border.color: theme.surfaceBorder
      }

      ColumnLayout {
        anchors.fill: parent
        anchors.margins: theme.panelPadding
        spacing: theme.smallGap

        RowLayout {
          Layout.fillWidth: true
          Item { Layout.fillWidth: true }
          Button {
            icon.name: "phone"
            display: AbstractButton.IconOnly
            checkable: true
            checked: root.phoneSettingsVisible
            Accessible.name: "iPhone settings"
            ToolTip.visible: hovered
            ToolTip.text: "iPhone settings"
            onToggled: root.phoneSettingsVisible = checked
          }
        }

        Label {
          visible: root.errorText !== "" || root.statusErrorText !== ""
          text: root.errorText !== "" ? root.errorText : root.statusErrorText
          textFormat: Text.PlainText
          color: theme.urgent
          wrapMode: Text.Wrap
          Layout.fillWidth: true
        }

        SplitView {
          visible: !root.phoneSettingsVisible
          Layout.fillWidth: true
          Layout.fillHeight: true

          ListView {
            id: threadList
            SplitView.preferredWidth: 250
            clip: true
            model: root.threads
            delegate: ItemDelegate {
              id: threadDelegate
              required property var modelData
              width: threadList.width
              highlighted: modelData.key === root.selectedThreadKey
              contentItem: Label {
                text: threadDelegate.modelData.name + "\n" +
                      (threadDelegate.modelData.messages.length
                        ? threadDelegate.modelData.messages[threadDelegate.modelData.messages.length - 1].body
                        : "")
                textFormat: Text.PlainText
                maximumLineCount: 2
                wrapMode: Text.Wrap
                elide: Text.ElideRight
              }
              onClicked: {
                root.selectedThreadKey = modelData.key
                root.confirmedGroupSignature = ""
              }
            }
          }

          ColumnLayout {
            id: conversationPane
            SplitView.fillWidth: true
            property var thread: root.selectedThread()

            Label {
              text: parent.thread ? parent.thread.name : "Select a conversation"
              textFormat: Text.PlainText
              font.bold: true
              Layout.fillWidth: true
            }
            ListView {
              id: messageList
              Layout.fillWidth: true
              Layout.fillHeight: true
              clip: true
              model: parent.thread ? parent.thread.messages : []
              delegate: Label {
                required property var modelData
                width: messageList.width
                padding: 8
                text: (modelData.outgoing ? "You\n" : "") + modelData.body
                textFormat: Text.PlainText
                wrapMode: Text.Wrap
                horizontalAlignment: modelData.outgoing ? Text.AlignRight : Text.AlignLeft
              }
            }
            CheckBox {
              id: confirmGroup
              property string signature: root.groupSignature(parent.thread)
              visible: parent.thread && parent.thread.is_group
              text: parent.thread ? "Confirm group: " + parent.thread.recipients.join(", ") : ""
              checked: signature !== "" && root.confirmedGroupSignature === signature
              onToggled: {
                if (checked) root.confirmedGroupSignature = signature
                else if (root.confirmedGroupSignature === signature) root.confirmedGroupSignature = ""
              }
              Layout.fillWidth: true
            }
            RowLayout {
              Layout.fillWidth: true
              TextField {
                id: composer
                Layout.fillWidth: true
                placeholderText: "Message"
                enabled: conversationPane.thread && conversationPane.thread.reply_ready
              }
              Button {
                text: "Send"
                enabled: composer.enabled && composer.text.trim() !== "" &&
                         (!conversationPane.thread.is_group ||
                          root.confirmedGroupSignature === root.groupSignature(conversationPane.thread)) &&
                         !sendProcess.running
                onClicked: {
                  var thread = conversationPane.thread
                  var args = ["/usr/bin/blueferry", "thread-send", thread.key, composer.text]
                  if (thread.is_group) args.push("--confirm-group")
                  sendProcess.command = args
                  sendProcess.running = true
                }
              }
            }
          }
        }

        ScrollView {
          id: iphoneScroll
          visible: root.phoneSettingsVisible
          Layout.fillWidth: true
          Layout.fillHeight: true
          contentWidth: availableWidth

          ColumnLayout {
            width: iphoneScroll.availableWidth
            spacing: 12

          Label {
            text: root.configured ? "Your iPhone" : "Connect an iPhone"
            font.pixelSize: theme.headingSize
            font.bold: true
          }
          Label {
            text: root.pairingStatus
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            Layout.fillWidth: true
            visible: !root.configured
          }
          Label {
            text: root.onboardingStage === "ready"
              ? "Bluetooth services and iPhone permissions have been verified."
              : root.onboardingStage === "ready-without-ancs"
                ? "Messages and contacts have been verified; per-app notifications are unavailable."
                : root.onboardingStage === "iphone-settings"
                    ? root.pendingIphoneSetupText()
                    : "Controller: " + (root.adapterName || "checking…")
            wrapMode: Text.Wrap
            Layout.fillWidth: true
          }
          Label {
            text: "Pair an iPhone"
            font.bold: true
            visible: !root.configured
          }
          Label {
            text: "Pairing takes two steps. Scan only finds nearby devices; it does not pair them. After scanning, select the iPhone and choose Pair Selected iPhone."
            wrapMode: Text.Wrap
            Layout.fillWidth: true
            visible: !root.configured
          }
          CheckBox {
            id: confirmBluetoothRestart
            visible: !root.configured && root.notificationsSupported && !root.bluezActive
            text: "I understand this briefly disconnects all Bluetooth devices"
          }
          Button {
            visible: !root.configured && root.notificationsSupported && !root.bluezActive
            text: bluezActivateProcess.running ? "Activating…" : "Activate Bluetooth support"
            enabled: confirmBluetoothRestart.checked && !bluezActivateProcess.running
            onClicked: {
              root.pairingStatus = "Authorizing Bluetooth restart…"
              bluezActivateProcess.running = true
              confirmBluetoothRestart.checked = false
            }
          }
          Button {
            visible: !root.configured
            text: deviceProcess.running ? "Scanning…" : "1. Scan for iPhone"
            enabled: !deviceProcess.running && !pairProcess.running
            onClicked: root.loadPairingDevices(true)
          }
          ComboBox {
            id: pairingDeviceCombo
            visible: !root.configured
            Layout.fillWidth: true
            model: root.pairingDevices
            textRole: "label"
          }
          Button {
            visible: !root.configured
            text: pairProcess.running ? "Pairing…"
              : root.selectedPairingDevice() && root.selectedPairingDevice().paired
                ? "Use existing pairing" : "2. Pair Selected iPhone"
            enabled: root.selectedPairingDevice() !== null
                     && root.pairingReady
                     && !deviceProcess.running && !pairProcess.running
            onClicked: {
              var device = root.selectedPairingDevice()
              root.pairingStatus = "Preparing secure pairing… The code can take about 15 seconds to appear."
              pairProcess.command = ["/usr/bin/blueferry", "pairing-complete", device.mac]
              pairProcess.running = true
            }
          }
          CheckBox {
            id: confirmForget
            text: "I will also forget this computer in the iPhone's Bluetooth settings"
            visible: root.configured
          }
          RowLayout {
            visible: root.configured
            Layout.fillWidth: true
            Label {
              Layout.fillWidth: true
              text: root.configuredPairingDevice()
                ? root.configuredPairingDevice().name : "iPhone"
              font.bold: true
            }
            Button {
              text: forgetProcess.running ? "Unpairing…" : "Unpair"
              enabled: confirmForget.checked && root.configuredMac !== "" && !forgetProcess.running
              onClicked: {
                forgetProcess.command = ["/usr/bin/blueferry", "pairing-forget", root.configuredMac]
                forgetProcess.running = true
                confirmForget.checked = false
              }
            }
          }
          Label {
            text: "Finish Setup on the iPhone"
            font.bold: true
            visible: root.configured && root.pendingIphoneSetupTasks().length > 0
          }
          Label {
            text: root.pendingIphoneSetupText()
            wrapMode: Text.Wrap
            Layout.fillWidth: true
            visible: root.configured && root.pendingIphoneSetupTasks().length > 0
          }
          Label {
            text: "Connection health"
            font.bold: true
          }
          Label {
            text: "Messages: " + (root.backendStatus.map ? "connected" : "unavailable")
              + "   Contacts: " + (root.backendStatus.pbap ? "connected" : "unavailable")
              + "   iPhone notifications: " + (root.backendStatus.ancs ? "connected" : "unavailable")
            wrapMode: Text.Wrap
            Layout.fillWidth: true
          }
          Label {
            text: !root.notificationsSupported
              ? "Bluetooth bearer API: not required"
              : root.bluezActive
                ? "Bluetooth bearer API: active"
                : "Bluetooth bearer API: restart required before pairing"
            color: root.bluezActive || !root.notificationsSupported
              ? theme.surfaceText : theme.warning
          }
          Label {
            text: "Desktop notifications"
            font.bold: true
          }
          Label {
            text: "Choose which iPhone events create desktop popups. Messages only is the default."
            wrapMode: Text.Wrap
            Layout.fillWidth: true
          }
          ComboBox {
            id: notificationPolicyCombo
            Layout.fillWidth: true
            model: ["All iPhone notifications", "Messages only", "None"]
            currentIndex: root.notificationPolicy === "all" ? 0
              : root.notificationPolicy === "none" ? 2 : 1
            enabled: root.configured && !notificationPolicyProcess.running
            onActivated: {
              var values = ["all", "messages", "none"]
              notificationPolicyProcess.command = [
                "/usr/bin/blueferry", "notification-policy-set", values[currentIndex]
              ]
              notificationPolicyProcess.running = true
            }
          }
          Label {
            text: "Local data"
            font.bold: true
          }
          Label {
            text: root.storageDetail
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            Layout.fillWidth: true
          }
          ComboBox {
            id: storagePolicyCombo
            Layout.fillWidth: true
            model: [
              "Encrypted with desktop keyring",
              "Unencrypted local data",
              "Do not retain local data"
            ]
            currentIndex: root.storagePolicy === "plaintext" ? 1
              : root.storagePolicy === "none" ? 2 : 0
            enabled: root.configured && !storagePolicyProcess.running
            onActivated: {
              var values = ["encrypted", "plaintext", "none"]
              if (values[currentIndex] !== root.storagePolicy
                  && !confirmStorageChange.checked) {
                root.errorText = "Confirm clearing local messages and contacts first"
                root.reload()
                return
              }
              storagePolicyProcess.command = [
                "/usr/bin/blueferry", "storage-policy-set", values[currentIndex]
              ]
              if (values[currentIndex] === "encrypted")
                root.storageUnlockAttempted = true
              storagePolicyProcess.running = true
              confirmStorageChange.checked = false
            }
          }
          CheckBox {
            id: confirmStorageChange
            text: "I understand that changing storage mode clears local messages and contacts"
          }
            Item { Layout.fillHeight: true }
          }
        }
      }
    }
  }
}
