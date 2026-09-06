pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io

ShellRoot {
  id: root
  property var threads: []
  property var contactResults: []
  property string contactQuery: ""
  property string contactsRequestedQuery: ""
  property string selectedThreadKey: ""
  property string pendingThreadKey: ""
  property string pendingMessageHandle: ""
  property var confirmedGroupSignatures: ({})
  property var groupParticipantsThread: null
  property var rosterChangedThread: null
  property var warnedRosterChanges: ({})
  property string errorText: ""
  property bool phoneSettingsVisible: false
  property var pairingDevices: []
  property string pairingStatus: "Step 1: scan for the iPhone. Scanning does not pair it."
  property bool bluezActive: false
  property bool hardwareSupported: false
  property bool notificationsSupported: false
  property bool ancsLimitedController: false
  property string controllerVendor: ""
  property bool compatibilityLoaded: false
  property bool ancsEnabled: true
  property bool compatibilityModeOverride: false
  property bool explicitPairingOverride: false
  property bool configured: false
  property bool targetSaved: false
  property bool targetBonded: false
  property bool bondStateKnown: false
  property string configuredMac: ""
  property string configuredAdapter: ""
  property bool pairingConfirmationPending: false
  property string pairingConfirmationPurpose: ""
  property string pairingPasskey: ""
  property bool pairingResultReceived: false
  property bool forgetResultReceived: false
  property string pairingIssueReport: ""
  property var pendingPairingDevice: null
  property string adapterName: ""
  property bool scanAfterCompatibility: false
  property var adapters: []
  property string onboardingStage: onboarding.stage
  property var backendStatus: ({})
  property string notificationPolicy: "messages"
  property bool contactsOnlyNotifications: false
  property string storagePolicy: "encrypted"
  property string storageState: "locked"
  property string storageDetail: ""
  property bool storageUnlockAttempted: false
  property string statusErrorText: ""
  property bool statusBusy: false
  property bool threadsBusy: false
  property bool contactsBusy: false
  property bool sendBusy: false
  property bool newMessageSendBusy: false
  property bool groupParticipantsBusy: false
  property bool deleteThreadsBusy: false
  property bool notificationPolicyBusy: false
  property bool contactsOnlyNotificationsBusy: false
  property bool storagePolicyBusy: false
  property bool storageUnlockBusy: false

  Theme { id: theme }
  OnboardingState {
    id: onboarding
    notificationsSupported: root.notificationsSupported
                            && root.ancsEnabled
                            && !root.compatibilityModeOverride
    bluezActive: root.bluezActive
    configured: root.configured
    backendStatus: root.backendStatus
  }

  Connections {
    target: Quickshell
    function onLastWindowClosed() { Qt.quit() }
  }

  function reload() {
    if (!configured) return
    if (!threadsBusy) {
      threadsBusy = true
      backendBridge.request("threads", {limit: 200})
    }
    if (!statusBusy) {
      statusBusy = true
      backendBridge.request("status", {})
    }
  }

  function searchContacts(query) {
    contactQuery = query.trim()
    if (contactQuery === "") {
      contactResults = []
      return
    }
    if (!contactsBusy) startContactSearch()
  }

  function startContactSearch() {
    if (contactQuery === "" || contactsBusy) return
    contactsRequestedQuery = contactQuery
    contactsBusy = true
    backendBridge.request("contacts", {query: contactsRequestedQuery})
  }

  function maybeUnlockStorage() {
    if (configured && storagePolicy === "encrypted" && storageState !== "ready"
        && !storageUnlockAttempted && !storageUnlockBusy) {
      storageUnlockAttempted = true
      storageUnlockBusy = true
      backendBridge.request("unlock_storage", {})
    }
  }

  function threadByKey(key) {
    for (var index = 0; index < threads.length; ++index) {
      if (threads[index].key === key) return threads[index]
    }
    return null
  }

  function selectedThread() {
    return threadByKey(selectedThreadKey)
  }

  function setThreadStarred(key, starred) {
    if (!key) return
    backendBridge.request("set_thread_starred", {
      thread_key: String(key),
      starred: starred === true
    })
  }

  function threadIsUnread(thread) {
    if (!thread) return false
    if (thread.unread === true) return true
    if (thread.unread === false) return false
    var messages = thread.messages || []
    for (var index = 0; index < messages.length; ++index) {
      if (!messages[index].outgoing && messages[index].read === false) return true
    }
    return false
  }

  function ancsLimited() {
    return root.backendStatus.ancs_limited_controller === true
      || root.ancsLimitedController
  }

  function ancsVendorName() {
    return String(root.backendStatus.controller_vendor || root.controllerVendor || "")
  }

  function ancsExpectedDetail() {
    var vendor = root.ancsVendorName()
    if (vendor !== "")
      return "Messages and contacts are connected. This " + vendor
        + " adapter does not support iPhone system notifications. Group texts will appear as separate messages from their sender."
    return "Messages and contacts are connected. This Bluetooth adapter does not support iPhone system notifications. Group texts will appear as separate messages from their sender."
  }

  function ancsUnavailableHint() {
    if (root.ancsLimited()) return root.ancsExpectedDetail()
    return "FYI: If ANCS remains unavailable, BlueZ may be retaining stale Bluetooth state. Before re-pairing, run sudo systemctl restart bluetooth.service, then forget this computer on the iPhone and pair again. This briefly disconnects all Bluetooth devices."
  }

  function markSelectedThreadRead() {
    if (!window.visible || !applicationSurface.Window.active || phoneSettingsVisible) return
    var thread = selectedThread()
    if (!thread || !threadIsUnread(thread)) return
    backendBridge.request("mark_thread_read", {thread_key: String(thread.key)})
  }

  onSelectedThreadKeyChanged: root.markSelectedThreadRead()
  onPhoneSettingsVisibleChanged: root.markSelectedThreadRead()

  function selectMessage(handle) {
    for (var threadIndex = 0; threadIndex < threads.length; ++threadIndex) {
      var thread = threads[threadIndex]
      for (var messageIndex = 0; messageIndex < thread.messages.length; ++messageIndex) {
        if (thread.messages[messageIndex].handle === handle) {
          selectedThreadKey = thread.key
          pendingMessageHandle = ""
          phoneSettingsVisible = false
          return true
        }
      }
    }
    return false
  }

  function presentWindow() {
    phoneSettingsVisible = false
    window.visible = true
  }

  function openThread(key) {
    pendingThreadKey = key
    selectedThreadKey = key
    pendingMessageHandle = ""
    presentWindow()
  }

  function openMessage(handle) {
    pendingMessageHandle = handle
    presentWindow()
    if (!selectMessage(handle)) reload()
  }

  function groupSignature(thread) {
    if (!thread || !thread.is_group) return ""
    var unique = []
    var recipients = thread.recipients || []
    for (var index = 0; index < recipients.length; ++index) {
      var address = String(recipients[index] || "")
      if (address !== "" && unique.indexOf(address) < 0) unique.push(address)
    }
    unique.sort()
    return [String(thread.roster_warning_id || "")].concat(unique).join("\n")
  }

  function groupIsConfirmed(thread) {
    if (!thread || !thread.is_group) return true
    if (thread.group_confirmed === true) return true
    var signature = root.groupSignature(thread)
    return signature !== "" &&
      (root.confirmedGroupSignatures[thread.key] || "") === signature
  }

  function setGroupConfirmed(thread, confirmed) {
    if (!thread || !thread.is_group) return
    var next = Object.assign({}, root.confirmedGroupSignatures)
    if (confirmed) {
      var signature = root.groupSignature(thread)
      if (signature === "") return
      next[thread.key] = signature
    } else {
      delete next[thread.key]
    }
    root.confirmedGroupSignatures = next
  }

  function participantLines(value) {
    var result = []
    var lines = value.split(/\r?\n/)
    for (var index = 0; index < lines.length; ++index) {
      var address = lines[index].trim()
      if (address !== "" && result.indexOf(address) < 0) result.push(address)
    }
    return result
  }

  function warnAboutRosterChanges() {
    for (var index = 0; index < threads.length; ++index) {
      var thread = threads[index]
      if (!thread.roster_changed) continue
      var warningId = thread.roster_warning_id
        || thread.key + ":" + (thread.unexpected_sender || "unknown")
      if (warnedRosterChanges[warningId] === true) continue
      warnedRosterChanges[warningId] = true
      rosterChangedThread = thread
      rosterChangedPopup.open()
      return
    }
  }

  function loadCompatibility(adapter) {
    if (compatibilityProcess.running) return
    compatibilityLoaded = false
    compatibilityProcess.command = adapter
      ? ["/usr/bin/blueferry", "pairing-compatibility-json", "--adapter", adapter]
      : ["/usr/bin/blueferry", "pairing-compatibility-json"]
    compatibilityProcess.running = true
  }

  function loadPairingDevices(scan) {
    if (deviceProcess.running) return
    var command = ["/usr/bin/blueferry", "pairing-devices-json"]
    if (scan) command.push("--scan-seconds", "24")
    if (root.adapterName) command.push("--adapter", root.adapterName)
    deviceProcess.command = command
    pairingStatus = scan ? "Scanning for Bluetooth devices…" : pairingStatus
    deviceProcess.running = true
  }

  function selectedPairingDevice() {
    var index = pairingDeviceCombo.currentIndex
    return index >= 0 && index < pairingDevices.length ? pairingDevices[index] : null
  }

  function startPairing(device, replaceSavedTarget) {
    pairingStatus = "Activating Bluetooth, then starting secure pairing…"
    pairingResultReceived = false
    pairProcess.command = [
      "/usr/bin/blueferry", "pairing-complete", device.mac, "--interactive-agent"
    ]
    if (root.adapterName)
      pairProcess.command.push("--adapter", root.adapterName)
    if (replaceSavedTarget)
      pairProcess.command.push("--replace-saved-mac", configuredMac)
    if (!notificationsSupported || compatibilityModeOverride)
      pairProcess.command.push("--compatibility-mode")
    if (explicitPairingOverride)
      pairProcess.command.push("--explicit-pairing")
    pairProcess.running = true
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
  }

  function markCompatibilityUnavailable(message) {
    hardwareSupported = false
    notificationsSupported = false
    compatibilityLoaded = true
    bluezActive = false
    adapterName = ""
    pairingStatus = message
  }

  function handlePairingLine(line) {
    if (line.trim() === "") return
    try {
      var parsed = JSON.parse(line)
      if (parsed.event === "display") {
        root.pairingPasskey = parsed.passkey || ""
        root.pairingStatus = "Compare the pairing code shown here with the iPhone."
        return
      }
      if (parsed.event === "confirmation") {
        root.pairingPasskey = parsed.passkey || ""
        root.pairingConfirmationPurpose = parsed.purpose || "pair"
        root.pairingConfirmationPending = true
        root.pairingStatus = root.pairingPasskey === ""
          ? "The iPhone is asking to pair. Approve only if you started this pairing."
          : "Confirm that this code exactly matches the code on the iPhone."
        return
      }
      if (!parsed.ok) {
        root.pairingResultReceived = true
        root.pairingStatus = parsed.error || "Pairing failed; it is safe to retry."
        root.pairingIssueReport = parsed.report_path || parsed.quirks_report || ""
        return
      }
      root.pairingResultReceived = true
      root.ancsEnabled = parsed.ancs_enabled !== false
      root.pairingStatus = parsed.ancs_ready || !root.ancsEnabled
        ? "Linux pairing complete. On the iPhone enable Show Message Notifications and Sync Contacts."
        : "Pairing is complete. Notification access is still settling; keep the iPhone Bluetooth settings open."
      root.pairingIssueReport = parsed.quirks_report || parsed.report_path || ""
      root.configured = true
      root.targetSaved = true
      root.targetBonded = true
      root.bondStateKnown = true
      root.configuredMac = parsed.device ? (parsed.device.mac || "") : ""
      root.configuredAdapter = root.adapterName
      if (parsed.device && parsed.device.adapter_path) {
        var parts = String(parsed.device.adapter_path).split("/")
        if (parts.length)
          root.configuredAdapter = parts[parts.length - 1]
      }
      root.loadPairingDevices(false)
      root.reload()
    } catch (error) {
      root.pairingStatus = "Pairing returned invalid data."
    }
  }

  function handleForgetLine(line) {
    if (line.trim() === "") return
    try {
      var parsed = JSON.parse(line)
      if (parsed.event === "confirmation" && parsed.purpose === "forget") {
        root.pairingPasskey = ""
        root.pairingConfirmationPurpose = "forget"
        root.pairingConfirmationPending = true
        root.pairingStatus = "Confirm that you want to unpair this iPhone."
        return
      }
      root.forgetResultReceived = true
      root.pairingConfirmationPending = false
      root.pairingConfirmationPurpose = ""
      root.pairingStatus = parsed.ok
        ? "Local bond removed. Also forget this computer on the iPhone, then scan again."
        : (parsed.error || "Could not remove the bond.")
      if (parsed.ok) {
        root.configured = false
        root.targetSaved = false
        root.targetBonded = false
        root.bondStateKnown = true
        root.configuredMac = ""
        root.configuredAdapter = ""
        root.ancsEnabled = true
        root.backendStatus = ({})
        root.pairingDevices = []
        root.loadPairingDevices(false)
      }
    } catch (error) { root.pairingStatus = "Forget operation returned invalid data." }
  }

  BackendBridge { id: backendBridge }

  IpcHandler {
    target: "blueferry"
    function open(): void { root.presentWindow() }
    function openThread(key: string): void { root.openThread(key) }
    function openMessage(handle: string): void { root.openMessage(handle) }
  }

  Connections {
    target: backendBridge

    function onResponse(method, requestId, result) {
      if (method === "status") {
        root.statusBusy = false
        if (typeof result !== "object" || result === null) {
          root.markStatusUnavailable("BlueFerry backend returned invalid status data")
          return
        }
        root.backendStatus = result
        var policy = result.notification_policy || "messages"
        root.notificationPolicy = ["all", "messages", "none"].indexOf(policy) >= 0
          ? policy : "messages"
        root.contactsOnlyNotifications =
          result.contacts_only_notifications === true
        var storagePolicy = result.storage_policy || "encrypted"
        root.storagePolicy = ["encrypted", "plaintext", "none"].indexOf(storagePolicy) >= 0
          ? storagePolicy : "encrypted"
        root.storageState = result.storage_state || "locked"
        root.storageDetail = result.storage_detail || "Storage status unavailable"
        root.statusErrorText = ""
        root.maybeUnlockStorage()
      } else if (method === "threads") {
        root.threadsBusy = false
        root.threads = Array.isArray(result) ? result : []
        if (root.pendingThreadKey !== "") {
          root.selectedThreadKey = root.pendingThreadKey
          if (root.selectedThread() !== null) root.pendingThreadKey = ""
        } else if (root.selectedThreadKey !== "" && root.selectedThread() === null) {
          root.selectedThreadKey = ""
        }
        if (root.pendingMessageHandle !== "")
          root.selectMessage(root.pendingMessageHandle)
        root.warnAboutRosterChanges()
        root.markSelectedThreadRead()
        root.errorText = ""
      } else if (method === "contacts") {
        root.contactsBusy = false
        if (root.contactsRequestedQuery === root.contactQuery)
          root.contactResults = Array.isArray(result) ? result : []
        else Qt.callLater(root.startContactSearch)
      } else if (method === "send_to_thread") {
        root.sendBusy = false
        composer.text = ""
        messageList.stickToBottom = true
        root.reload()
      } else if (method === "send") {
        root.newMessageSendBusy = false
        newMessagePopup.close()
        newRecipient.text = ""
        newMessageBody.text = ""
        root.reload()
      } else if (method === "set_group_participants") {
        root.groupParticipantsBusy = false
        if (root.groupParticipantsThread)
          root.setGroupConfirmed(root.groupParticipantsThread, false)
        groupParticipantsPopup.close()
        root.reload()
      } else if (method === "mark_thread_read") {
        // HistoryChanged reloads the thread list.
      } else if (method === "set_thread_starred") {
        // HistoryChanged reloads the thread list.
      } else if (method === "delete_threads") {
        root.deleteThreadsBusy = false
        root.reload()
      } else if (method === "set_notification_policy") {
        root.notificationPolicyBusy = false
        root.notificationPolicy = String(result)
        root.reload()
      } else if (method === "set_contacts_only_notifications") {
        root.contactsOnlyNotificationsBusy = false
        root.contactsOnlyNotifications = result === true
        root.reload()
      } else if (method === "set_storage_policy") {
        root.storagePolicyBusy = false
        if (typeof result === "object" && result !== null) {
          root.storagePolicy = result.storage_policy || root.storagePolicy
          root.storageState = result.storage_state || root.storageState
          root.storageDetail = result.storage_detail || root.storageDetail
        }
        root.reload()
      } else if (method === "unlock_storage") {
        root.storageUnlockBusy = false
        if (typeof result === "object" && result !== null) {
          root.storageState = result.storage_state || root.storageState
          root.storageDetail = result.storage_detail || root.storageDetail
        }
        root.reload()
      }
    }

    function onFailure(method, requestId, message) {
      if (method === "status") {
        root.statusBusy = false
        root.markStatusUnavailable("BlueFerry backend is unavailable")
      } else if (method === "threads") {
        root.threadsBusy = false
        root.errorText = "BlueFerry daemon is unavailable"
      } else if (method === "contacts") {
        root.contactsBusy = false
        root.errorText = message || "Contact search failed"
        if (root.contactsRequestedQuery !== root.contactQuery)
          Qt.callLater(root.startContactSearch)
      } else if (method === "send_to_thread") {
        root.sendBusy = false
        root.errorText = message
      } else if (method === "send") {
        root.newMessageSendBusy = false
        root.errorText = message
      } else if (method === "set_group_participants") {
        root.groupParticipantsBusy = false
        root.errorText = message
      } else if (method === "mark_thread_read") {
        // Keep the conversation open even if MAP write-back fails.
      } else if (method === "set_thread_starred") {
        root.errorText = message || "Could not update starred conversations"
      } else if (method === "delete_threads") {
        root.deleteThreadsBusy = false
        root.errorText = message || "Could not delete local conversations"
      } else if (method === "set_notification_policy") {
        root.notificationPolicyBusy = false
        root.errorText = message || "Could not save notification preference"
      } else if (method === "set_contacts_only_notifications") {
        root.contactsOnlyNotificationsBusy = false
        root.errorText = message || "Could not save notification preference"
        root.reload()
      } else if (method === "set_storage_policy") {
        root.storagePolicyBusy = false
        root.errorText = message
      } else if (method === "unlock_storage") {
        root.storageUnlockBusy = false
        root.errorText = message
      } else {
        root.statusBusy = false
        root.threadsBusy = false
        root.contactsBusy = false
        root.sendBusy = false
        root.newMessageSendBusy = false
        root.groupParticipantsBusy = false
        root.deleteThreadsBusy = false
        root.notificationPolicyBusy = false
        root.contactsOnlyNotificationsBusy = false
        root.storagePolicyBusy = false
        root.storageUnlockBusy = false
        root.errorText = message
      }
    }

    function onEventReceived(name, data) {
      if (name === "open-message") root.openMessage(String(data || ""))
      else if (name === "history-changed" || name === "status-changed") root.reload()
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
          root.ancsLimitedController = parsed.ancs_limited_controller === true
          root.controllerVendor = parsed.controller_vendor || ""
          root.compatibilityLoaded = true
          root.bluezActive = parsed.bearer_api_active === true
          root.adapterName = parsed.adapter || ""
          root.adapters = Array.isArray(parsed.adapters) ? parsed.adapters : []
          if (adapterCombo.count > 0) {
            for (var index = 0; index < root.adapters.length; ++index) {
              if (root.adapters[index].name === root.adapterName) {
                adapterCombo.currentIndex = index
                break
              }
            }
          }
          if (!root.hardwareSupported) {
            root.pairingStatus = (parsed.issue || "Bluetooth controller capabilities could not be verified.")
              + " Pairing is still available in compatibility mode."
          }
          var scan = root.scanAfterCompatibility
          root.scanAfterCompatibility = false
          root.loadPairingDevices(scan)
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
          root.targetSaved = parsed.saved === true
          root.bondStateKnown = typeof parsed.bonded === "boolean"
          root.targetBonded = parsed.bonded === true
          root.configuredMac = root.targetSaved ? (parsed.mac || "") : ""
          root.configuredAdapter = root.targetSaved ? (parsed.adapter || "") : ""
          root.ancsEnabled = parsed.ancs_enabled !== false
          if (typeof parsed.pairing_issue_report === "string")
            root.pairingIssueReport = parsed.pairing_issue_report
          if (root.targetSaved && root.bondStateKnown && !root.targetBonded)
            root.pairingStatus = "This phone is no longer paired in BlueZ. Clear the saved phone, then scan and pair again."
          if (!root.configured) root.phoneSettingsVisible = true
          else root.reload()
        } catch (error) { root.phoneSettingsVisible = true }
      }
    }
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
          if (root.targetSaved && root.bondStateKnown && !root.targetBonded) {
            root.pairingStatus = "This phone is no longer paired in BlueZ. Clear the saved phone, then scan and pair again."
          } else root.pairingStatus = matching.length
            ? root.configured
              ? "Linux pairing is complete. Check the required iPhone settings below."
              : "Step 2: select the iPhone, then choose Pair Selected iPhone."
            : "No devices found. Unlock the iPhone, keep Bluetooth settings open, and scan again."
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
            root.scanAfterCompatibility = true
            root.loadCompatibility(root.adapterName)
          } else {
            root.pairingStatus = parsed.error || "Bluetooth activation failed."
          }
        } catch (error) { root.pairingStatus = "Bluetooth activation returned invalid data." }
      }
    }
  }

  Process {
    id: pairProcess
    stdinEnabled: true
    stdout: SplitParser {
      onRead: function(line) { root.handlePairingLine(line) }
    }
    stderr: StdioCollector {
      onStreamFinished: if (text.trim() !== "") root.pairingStatus = text.trim()
    }
    // qmllint disable signal-handler-parameters
    onExited: function(code) {
      root.pairingConfirmationPending = false
      root.pairingConfirmationPurpose = ""
      root.pairingPasskey = ""
      if (code !== 0 && !root.pairingResultReceived)
        root.pairingStatus = "Pairing did not complete; unlock the iPhone and try again."
    }
  }

  Process {
    id: pairingIssueUrlProcess
    command: ["/usr/bin/blueferry", "pairing-issue", "--print-url"]
    stdout: StdioCollector {
      onStreamFinished: {
        var url = text.trim().split("\n").pop()
        if (url.indexOf("https://") === 0)
          Qt.openUrlExternally(url)
      }
    }
  }

  Process {
    id: forgetProcess
    stdinEnabled: true
    stdout: SplitParser {
      onRead: function(line) { root.handleForgetLine(line) }
    }
    // qmllint disable signal-handler-parameters
    onExited: function(code) {
      if (root.pairingConfirmationPurpose === "forget") {
        root.pairingConfirmationPending = false
        root.pairingConfirmationPurpose = ""
      }
      if (code !== 0 && !root.forgetResultReceived)
        root.pairingStatus = "Could not remove the bond."
    }
  }

  Timer {
    interval: 3000
    repeat: true
    running: true
    triggeredOnStart: true
    onTriggered: {
      root.reload()
      if (!configurationProcess.running) configurationProcess.running = true
    }
  }

  Component.onCompleted: {
    compatibilityProcess.running = true
    configurationProcess.running = true
    loadPairingDevices(false)
    var messageHandle = String(Quickshell.env("BLUEFERRY_OPEN_MESSAGE_HANDLE") || "")
    var threadKey = String(Quickshell.env("BLUEFERRY_OPEN_THREAD_KEY") || "")
    if (messageHandle !== "") root.openMessage(messageHandle)
    else if (threadKey !== "") root.openThread(threadKey)
  }

  FloatingWindow {
    id: window
    title: "BlueFerry"
    implicitWidth: 900
    implicitHeight: 660
    color: theme.windowSurface

    Pane {
      id: applicationSurface
      Window.onActiveChanged: root.markSelectedThreadRead()
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
        radius: theme.panelRadius
        color: theme.windowSurface
      }

      ColumnLayout {
        anchors.fill: parent
        anchors.margins: theme.panelPadding
        spacing: theme.scaled(14)

        FerryLabel {
          visible: root.errorText !== "" || root.statusErrorText !== ""
          text: root.errorText !== "" ? root.errorText : root.statusErrorText
          textFormat: Text.PlainText
          color: theme.urgent
          wrapMode: Text.Wrap
          Layout.fillWidth: true
          leftPadding: theme.scaled(14)
          rightPadding: theme.scaled(14)
          topPadding: theme.scaled(10)
          bottomPadding: theme.scaled(10)

          background: Rectangle {
            color: Qt.rgba(theme.urgent.r, theme.urgent.g, theme.urgent.b, 0.12)
            border.color: theme.urgent
            radius: theme.controlRadius
          }
        }

        Rectangle {
          Layout.fillWidth: true
          implicitHeight: mapRefusedLabel.implicitHeight + theme.scaled(16)
          visible: !root.phoneSettingsVisible && onboarding.mapConnectionRefused()
          color: Qt.rgba(theme.warning.r, theme.warning.g, theme.warning.b, 0.14)
          border.color: theme.warning
          radius: theme.controlRadius

          FerryLabel {
            id: mapRefusedLabel
            anchors.fill: parent
            anchors.margins: theme.scaled(8)
            text: "iPhone is refusing message connections; is it connected to another computer?"
            textFormat: Text.PlainText
            color: theme.windowText
            font.bold: true
            wrapMode: Text.Wrap
          }
        }

        SplitView {
          visible: !root.phoneSettingsVisible
          Layout.fillWidth: true
          Layout.fillHeight: true
          handle: Rectangle {
            implicitWidth: theme.scaled(14)
            color: "transparent"
            Rectangle {
              anchors.centerIn: parent
              width: 1
              height: parent.height - theme.scaled(24)
              color: theme.divider
            }
          }

          Rectangle {
            SplitView.preferredWidth: theme.scaled(250)
            SplitView.minimumWidth: theme.scaled(190)
            color: theme.cardSurface
            radius: theme.panelRadius
            border.color: theme.divider

            ColumnLayout {
              anchors.fill: parent
              anchors.margins: theme.scaled(10)
              spacing: theme.scaled(6)

              RowLayout {
                Layout.fillWidth: true
                FerryLabel {
                  Layout.fillWidth: true
                  text: "CONVERSATIONS"
                  color: theme.muted
                  font.family: theme.fontFamily
                  font.pixelSize: theme.captionSize
                  font.bold: true
                  font.letterSpacing: 1
                  leftPadding: theme.scaled(4)
                }
                FerryButton {
                  text: "+"
                  implicitWidth: implicitHeight
                  highlighted: true
                  Accessible.name: "New message"
                  ToolTip.visible: hovered
                  ToolTip.text: "New message"
                  onClicked: newMessagePopup.open()
                }
              }

              ListView {
                id: threadList
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                spacing: theme.scaled(5)
                model: root.threads
                delegate: ItemDelegate {
                  id: threadDelegate
                  required property var modelData
                  width: threadList.width
                  implicitHeight: theme.scaled(62)
                  highlighted: modelData.key === root.selectedThreadKey
                  leftPadding: theme.scaled(8)
                  rightPadding: theme.scaled(8)
                  onClicked: root.selectedThreadKey = modelData.key
                  contentItem: Row {
                    clip: true
                    spacing: theme.scaled(10)

                    Rectangle {
                      width: theme.scaled(34)
                      height: width
                      anchors.verticalCenter: parent.verticalCenter
                      radius: width / 2
                      color: threadDelegate.highlighted
                        ? theme.primarySurface : theme.raisedSurface
                      Text {
                        anchors.centerIn: parent
                        text: threadDelegate.modelData.is_group ? "#"
                          : String(threadDelegate.modelData.name || "?").charAt(0).toUpperCase()
                        textFormat: Text.PlainText
                        color: threadDelegate.highlighted
                          ? theme.primaryText : theme.windowText
                        font.family: theme.fontFamily
                        font.pixelSize: theme.baseFontSize
                        font.bold: true
                      }
                    }

                    Column {
                      id: threadContent
                      width: parent.width - x - theme.scaled(28)
                      anchors.verticalCenter: parent.verticalCenter
                      spacing: theme.scaled(3)
                      Text {
                        width: parent.width
                        text: threadDelegate.modelData.name
                        textFormat: Text.PlainText
                        color: theme.windowText
                        font.family: theme.fontFamily
                        font.pixelSize: theme.baseFontSize
                        font.bold: root.threadIsUnread(threadDelegate.modelData)
                        wrapMode: Text.NoWrap
                        maximumLineCount: 1
                        elide: Text.ElideRight
                        clip: true
                      }
                      QuickshellThreadPreview {
                        width: parent.width
                        thread: threadDelegate.modelData
                        ferryTheme: theme
                      }
                    }
                    Text {
                      anchors.verticalCenter: parent.verticalCenter
                      text: threadDelegate.modelData.starred ? "★" : "☆"
                      textFormat: Text.PlainText
                      color: threadDelegate.modelData.starred
                        ? theme.accent : theme.muted
                      font.family: theme.fontFamily
                      font.pixelSize: theme.baseFontSize
                      Accessible.name: threadDelegate.modelData.starred
                        ? "Unstar conversation" : "Star conversation"
                      MouseArea {
                        anchors.fill: parent
                        anchors.margins: theme.scaled(-6)
                        cursorShape: Qt.PointingHandCursor
                        onClicked: {
                          root.setThreadStarred(
                            threadDelegate.modelData.key,
                            !threadDelegate.modelData.starred
                          )
                        }
                      }
                    }
                  }
                  background: Rectangle {
                    color: threadDelegate.highlighted ? theme.selectedSurface
                      : threadDelegate.hovered ? theme.hoverSurface : "transparent"
                    border.color: threadDelegate.highlighted ? theme.divider : "transparent"
                    radius: theme.controlRadius
                  }
                  TapHandler {
                    acceptedButtons: Qt.RightButton
                    onTapped: eventPoint => {
                      threadContextMenu.threadKey = threadDelegate.modelData.key
                      threadContextMenu.popup(
                        threadDelegate,
                        eventPoint.position.x,
                        eventPoint.position.y
                      )
                    }
                  }
                }

                Column {
                  anchors.centerIn: parent
                  visible: threadList.count === 0
                  spacing: theme.scaled(6)
                  FerryLabel {
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: "NO CONVERSATIONS"
                    color: theme.windowText
                    font.bold: true
                    font.letterSpacing: 1
                  }
                  FerryLabel {
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: "New messages will dock here."
                    color: theme.muted
                    font.pixelSize: theme.captionSize
                  }
                }
              }

              RowLayout {
                Layout.fillWidth: true
                FerryButton {
                  text: "SETTINGS"
                  labelSize: theme.captionSize
                  labelBold: true
                  labelLetterSpacing: 1
                  bare: true
                  subtle: true
                  leftPadding: theme.scaled(4)
                  Accessible.name: "iPhone settings"
                  ToolTip.visible: hovered
                  ToolTip.text: "iPhone settings"
                  onClicked: root.phoneSettingsVisible = true
                }
                Item { Layout.fillWidth: true }
              }
            }
          }

          Rectangle {
            id: conversationPane
            SplitView.fillWidth: true
            SplitView.minimumWidth: theme.scaled(320)
            property var thread: root.selectedThread()
            color: theme.cardSurface
            radius: theme.panelRadius
            border.color: theme.divider

            ColumnLayout {
              anchors.fill: parent
              anchors.margins: theme.scaled(12)
              spacing: theme.scaled(10)

              RowLayout {
                Layout.fillWidth: true
                FerryLabel {
                  Layout.fillWidth: true
                  text: conversationPane.thread
                    ? conversationPane.thread.name : "SELECT A CONVERSATION"
                  textFormat: Text.PlainText
                  color: conversationPane.thread ? theme.windowText : theme.muted
                  font.bold: true
                  font.pixelSize: theme.headingSize
                  elide: Text.ElideRight
                }
                FerryButton {
                  visible: conversationPane.thread
                    && conversationPane.thread.group_origin === "named"
                  text: "Members"
                  bare: true
                  onClicked: {
                    root.groupParticipantsThread = conversationPane.thread
                    groupParticipantsPopup.open()
                  }
                }
                FerryLabel {
                  visible: conversationPane.thread !== null
                  text: conversationPane.thread && conversationPane.thread.is_group
                    ? "GROUP" : "DIRECT"
                  color: theme.muted
                  font.pixelSize: theme.captionSize
                  font.bold: true
                  font.letterSpacing: 1
                }
              }

              Rectangle {
                Layout.fillWidth: true
                implicitHeight: 1
                color: theme.divider
              }

              Label {
                Layout.fillWidth: true
                visible: conversationPane.thread !== null
                  && conversationPane.thread.messages_truncated === true
                text: "Showing recent messages. Older messages remain in local history."
                wrapMode: Text.Wrap
                color: theme.windowText
              }

              ListView {
                id: messageList
                property bool stickToBottom: true
                property string threadKey: conversationPane.thread
                  ? conversationPane.thread.key : ""

                function scrollToBottom() {
                  if (stickToBottom && count > 0) positionViewAtEnd()
                }

                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                spacing: theme.scaled(8)
                model: conversationPane.thread ? conversationPane.thread.messages : []
                onThreadKeyChanged: {
                  stickToBottom = true
                  Qt.callLater(scrollToBottom)
                }
                onCountChanged: Qt.callLater(scrollToBottom)
                onContentHeightChanged: Qt.callLater(scrollToBottom)
                onMovementStarted: stickToBottom = false
                onMovementEnded: stickToBottom = atYEnd
                delegate: Item {
                  id: messageRow
                  required property var modelData
                  width: messageList.width
                  height: bubble.height > 0
                    ? bubble.height + theme.scaled(3) : 0

                  QuickshellMessageBubble {
                    id: bubble
                    message: messageRow.modelData
                    availableWidth: messageList.width
                    availableHeight: messageList.height
                    showSender: conversationPane.thread
                      && conversationPane.thread.is_group
                    ferryTheme: theme
                    anchors.right: messageRow.modelData.outgoing ? parent.right : undefined
                    anchors.left: messageRow.modelData.outgoing ? undefined : parent.left
                  }
                }

                Column {
                  anchors.centerIn: parent
                  visible: conversationPane.thread === null
                  spacing: theme.scaled(8)
                  FerryLabel {
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: "✦"
                    color: theme.muted
                    font.pixelSize: theme.displaySize
                  }
                  FerryLabel {
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: "PICK A THREAD"
                    color: theme.windowText
                    font.bold: true
                    font.letterSpacing: 1
                  }
                  FerryLabel {
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: "Your messages stay local to this machine."
                    color: theme.muted
                    font.pixelSize: theme.captionSize
                  }
                }
              }

              Rectangle {
                Layout.fillWidth: true
                implicitHeight: groupRosterPrompt.implicitHeight + theme.scaled(20)
                visible: conversationPane.thread
                  && conversationPane.thread.participants_required === true
                color: theme.raisedSurface
                border.color: theme.accent
                radius: theme.controlRadius

                RowLayout {
                  id: groupRosterPrompt
                  anchors.left: parent.left
                  anchors.right: parent.right
                  anchors.verticalCenter: parent.verticalCenter
                  anchors.margins: theme.scaled(10)
                  spacing: theme.scaled(10)

                  FerryLabel {
                    Layout.fillWidth: true
                    wrapMode: Text.Wrap
                    text: conversationPane.thread
                      ? conversationPane.thread.roster_changed
                        ? (conversationPane.thread.unexpected_sender || "Someone new")
                          + " is not in BlueFerry's saved participant list for "
                          + conversationPane.thread.name
                          + ". Review the list before replying."
                        : (conversationPane.thread.prompt_sender || "Someone")
                          + " has sent a message to the group "
                          + conversationPane.thread.name
                          + ". BlueFerry needs its participant list before you can reply."
                      : ""
                  }
                  FerryButton {
                    text: "Add participants"
                    highlighted: true
                    onClicked: {
                      root.groupParticipantsThread = conversationPane.thread
                      groupParticipantsPopup.open()
                    }
                  }
                }
              }

              FerryCheckBox {
                id: confirmGroup
                property var thread: conversationPane.thread
                property string signature: root.groupSignature(thread)
                visible: thread && thread.is_group && thread.reply_ready
                text: thread
                  ? "Confirm group: " + thread.recipients.join(", ") : ""
                checked: root.groupIsConfirmed(thread) && signature !== ""
                enabled: !(thread && thread.group_confirmed === true)
                onToggled: root.setGroupConfirmed(thread, checked)
                Layout.fillWidth: true
              }

              Rectangle {
                Layout.fillWidth: true
                implicitHeight: composerRow.implicitHeight + theme.scaled(12)
                color: theme.raisedSurface
                border.color: theme.divider
                radius: theme.controlRadius

                RowLayout {
                  id: composerRow
                  anchors.fill: parent
                  anchors.margins: theme.scaled(6)
                  FerryMessageComposer {
                    id: composer
                    Layout.fillWidth: true
                    placeholderText: "Write a message…"
                    flat: true
                    enabled: conversationPane.thread && conversationPane.thread.reply_ready
                    onAccepted: {
                      if (sendMessageButton.enabled) sendMessageButton.clicked()
                    }
                  }
                  FerryButton {
                    id: sendMessageButton
                    Layout.alignment: Qt.AlignBottom
                    text: root.sendBusy ? "SENDING" : "SEND"
                    highlighted: true
                    enabled: composer.enabled && composer.text.trim() !== "" &&
                             root.groupIsConfirmed(conversationPane.thread) &&
                             !root.sendBusy
                    onClicked: {
                      var thread = conversationPane.thread
                      root.sendBusy = true
                      backendBridge.request("send_to_thread", {
                        thread_key: thread.key,
                        body: composer.text,
                        confirm_group: thread.is_group
                      })
                    }
                  }
                }
              }
            }
          }
        }

        Rectangle {
          id: settingsDeck
          visible: root.phoneSettingsVisible
          Layout.fillWidth: true
          Layout.fillHeight: true
          color: theme.cardSurface
          radius: theme.panelRadius
          border.color: theme.divider

          ColumnLayout {
            anchors.fill: parent
            anchors.margins: theme.scaled(10)
            spacing: theme.scaled(6)

            ScrollView {
              id: iphoneScroll
              Layout.fillWidth: true
              Layout.fillHeight: true
              contentWidth: availableWidth

              ColumnLayout {
                width: Math.min(iphoneScroll.availableWidth, theme.scaled(620))
                x: Math.max(0, (iphoneScroll.availableWidth - width) / 2)
                spacing: theme.scaled(12)

          FerrySectionLabel {
            text: "iPhone settings"
            topPadding: 0
          }
          FerryLabel {
            text: root.configured ? "Your iPhone" : "Connect an iPhone"
            font.pixelSize: theme.displaySize
            font.bold: true
          }
          Rectangle {
            Layout.fillWidth: true
            implicitHeight: 1
            color: theme.divider
          }
          FerryLabel {
            text: root.pairingStatus
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            Layout.fillWidth: true
            visible: !root.configured
          }
          FerryLabel {
            text: root.onboardingStage === "ready"
              ? "Bluetooth services and iPhone permissions have been verified."
              : root.onboardingStage === "ready-without-ancs"
                ? (root.ancsLimited()
                  ? root.ancsExpectedDetail()
                  : "Messages and contacts have been verified. System notifications are unavailable, so group texts may appear as individual conversations.")
                : root.onboardingStage === "iphone-settings"
                    ? onboarding.mapConnectionRefused()
                      ? "Cannot retrieve or send messages - are you connected to another computer? We will reconnect once your phone is free"
                      : "Connected. Finish the remaining iPhone permissions below."
                    : "Controller: " + (root.adapterName || "checking…")
            wrapMode: Text.Wrap
            Layout.fillWidth: true
          }
          FerrySectionLabel {
            text: "Pair an iPhone"
            visible: !root.configured
          }
          FerryLabel {
            text: "Scan for and select your iPhone here, then choose Pair. When the pairing request appears on the iPhone, approve it and confirm that the codes match. Pairing may appear idle for up to 15 seconds. After it completes, return to the Bluetooth device list and open this computer's ⓘ page a few times; turn on any new toggles that appear. System Notification access is also how BlueFerry recognizes group text threads; without it, a group text appears as a one-to-one conversation with its sender."
            wrapMode: Text.Wrap
            Layout.fillWidth: true
            visible: !root.configured
          }
          FerryCheckBox {
            id: confirmBluetoothRestart
            visible: !root.configured
                     && root.notificationsSupported && !root.bluezActive
                     && !compatibilityMode.checked
            text: "I understand this briefly disconnects all Bluetooth devices"
          }
          FerryButton {
            visible: !root.configured
                     && root.notificationsSupported && !root.bluezActive
                     && !compatibilityMode.checked
            text: bluezActivateProcess.running ? "Activating…" : "Activate Bluetooth support"
            enabled: confirmBluetoothRestart.checked && !bluezActivateProcess.running
            onClicked: {
              root.pairingStatus = "Authorizing Bluetooth restart…"
              bluezActivateProcess.running = true
              confirmBluetoothRestart.checked = false
            }
          }
          FerryComboBox {
            id: adapterCombo
            visible: !root.configured && root.adapters.length > 1
            Layout.fillWidth: true
            model: root.adapters
            textRole: "label"
            onActivated: {
              var option = root.adapters[currentIndex]
              if (option && option.name && option.name !== root.adapterName)
                root.loadCompatibility(option.name)
            }
          }
          FerryButton {
            visible: !root.configured
            text: deviceProcess.running ? "Scanning…" : "1. Scan for iPhone"
            enabled: !deviceProcess.running && !pairProcess.running
            onClicked: root.loadPairingDevices(true)
          }
          FerryComboBox {
            id: pairingDeviceCombo
            visible: !root.configured
            Layout.fillWidth: true
            model: root.pairingDevices
            textRole: "label"
          }
          FerryCheckBox {
            id: compatibilityMode
            visible: !root.configured
            text: "Compatibility pairing for iOS 18 or earlier"
            checked: root.compatibilityLoaded
                     && (!root.notificationsSupported || root.compatibilityModeOverride)
            enabled: root.compatibilityLoaded && root.notificationsSupported
                     && !pairProcess.running
            onClicked: root.compatibilityModeOverride = checked
          }
          FerryCheckBox {
            id: explicitPairing
            visible: !root.configured
            text: "Use explicit Bluetooth pairing"
            checked: root.explicitPairingOverride
            enabled: !pairProcess.running
            onClicked: root.explicitPairingOverride = checked
          }
          FerryLabel {
            visible: !root.configured && compatibilityMode.checked
            text: "BlueFerry will still advertise ANCS solicitation so Messages and Contacts permissions appear, but it will not connect system notifications."
            wrapMode: Text.Wrap
            Layout.fillWidth: true
          }
          FerryButton {
            visible: !root.configured
            text: pairProcess.running ? "Pairing…"
              : root.selectedPairingDevice() && root.selectedPairingDevice().paired
                ? "Use existing pairing" : "2. Pair Selected iPhone"
            enabled: root.selectedPairingDevice() !== null
                     && root.compatibilityLoaded
                     && !deviceProcess.running && !pairProcess.running
                     && !forgetProcess.running
            onClicked: {
              var device = root.selectedPairingDevice()
              if (!device.paired && root.targetSaved) {
                root.pendingPairingDevice = device
                replaceTargetPopup.open()
              } else root.startPairing(device, false)
            }
          }
          FerryButton {
            visible: root.pairingIssueReport !== ""
            text: "Report Pairing Issue"
            onClicked: pairingIssuePopup.open()
          }
          FerryLabel {
            text: root.pairingConfirmationPurpose === "forget"
              ? "Confirm unpairing"
              : root.pairingPasskey === "" ? "Pairing confirmation" : root.pairingPasskey
            font.bold: true
            font.pixelSize: root.pairingPasskey === "" ? theme.baseFontSize : theme.headingSize
            horizontalAlignment: Text.AlignHCenter
            Layout.fillWidth: true
            visible: root.pairingConfirmationPending
          }
          RowLayout {
            Layout.fillWidth: true
            visible: root.pairingConfirmationPending
            FerryButton {
              text: root.pairingConfirmationPurpose === "forget"
                ? "Cancel" : "Cancel Pairing"
              Layout.fillWidth: true
              onClicked: {
                var forgetting = root.pairingConfirmationPurpose === "forget"
                if (forgetting)
                  forgetProcess.write("no\n")
                else pairProcess.write("no\n")
                root.pairingConfirmationPending = false
                root.pairingConfirmationPurpose = ""
                root.pairingStatus = forgetting
                  ? "Canceling unpair…" : "Canceling pairing…"
              }
            }
            FerryButton {
              text: root.pairingConfirmationPurpose === "forget"
                ? "Unpair"
                : root.pairingPasskey === "" ? "Approve Pairing" : "Codes Match"
              Layout.fillWidth: true
              highlighted: true
              onClicked: {
                var forgetting = root.pairingConfirmationPurpose === "forget"
                if (forgetting)
                  forgetProcess.write("yes\n")
                else pairProcess.write("yes\n")
                root.pairingConfirmationPending = false
                root.pairingConfirmationPurpose = ""
                root.pairingStatus = forgetting
                  ? "Removing the local bond…" : "Finishing Bluetooth setup…"
              }
            }
          }
          RowLayout {
            visible: root.targetSaved
            Layout.fillWidth: true
            FerryLabel {
              Layout.fillWidth: true
              text: root.configuredPairingDevice()
                ? root.configuredPairingDevice().name : "iPhone"
              font.bold: true
            }
            FerryButton {
              text: forgetProcess.running ? "Removing…"
                : root.bondStateKnown && !root.targetBonded
                  ? "Clear Saved Phone" : "Unpair"
              enabled: root.configuredMac !== ""
                && !forgetProcess.running && !pairProcess.running
              onClicked: {
                root.forgetResultReceived = false
                forgetProcess.command = [
                  "/usr/bin/blueferry", "pairing-forget", root.configuredMac,
                  "--interactive-approval"
                ]
                if (root.configuredAdapter)
                  forgetProcess.command.push("--adapter", root.configuredAdapter)
                forgetProcess.running = true
              }
            }
          }
          FerrySectionLabel {
            text: "Finish Setup on the iPhone"
            visible: root.configured && onboarding.pendingIphoneSetupTasks().length > 0
          }
          FerryLabel {
            text: onboarding.pendingIphoneSetupText()
            wrapMode: Text.Wrap
            Layout.fillWidth: true
            visible: root.configured && onboarding.pendingIphoneSetupTasks().length > 0
          }
          FerrySectionLabel {
            text: "Connection health"
          }
          FerryInfoRow {
            label: "Messages"
            value: onboarding.mapConnectionRefused()
              ? "Connection refused"
              : root.backendStatus.map ? "Connected" : "Unavailable"
            Layout.fillWidth: true
          }
          FerryLabel {
            visible: onboarding.mapConnectionRefused()
            text: "iPhone is refusing message connections; is it connected to another computer?"
            textFormat: Text.PlainText
            color: theme.warning
            wrapMode: Text.Wrap
            Layout.fillWidth: true
          }
          FerryInfoRow {
            label: "Contacts"
            value: root.backendStatus.pbap ? "Connected" : "Unavailable"
            Layout.fillWidth: true
          }
          FerryInfoRow {
            label: "Notifications"
            value: root.backendStatus.ancs ? "Connected" : "Unavailable"
            Layout.fillWidth: true
          }
          FerryLabel {
            visible: root.configured
              && onboarding.notificationsSupported
              && root.backendStatus.map
              && root.backendStatus.pbap
              && !root.backendStatus.ancs
            text: root.ancsUnavailableHint()
            color: root.ancsLimited() ? theme.surfaceText : theme.warning
            wrapMode: Text.Wrap
            Layout.fillWidth: true
          }
          FerryLabel {
            text: !root.notificationsSupported
              ? "Bluetooth bearer API: not required"
              : root.bluezActive
                ? "Bluetooth bearer API: active"
                : "Bluetooth bearer API: restart required before pairing"
            color: root.bluezActive || !root.notificationsSupported
              ? theme.surfaceText : theme.warning
          }
          FerrySectionLabel {
            text: "Desktop notifications"
          }
          FerryComboBox {
            id: notificationPolicyCombo
            Layout.fillWidth: true
            model: ["All iPhone notifications", "Messages only", "None"]
            currentIndex: root.notificationPolicy === "all" ? 0
              : root.notificationPolicy === "none" ? 2 : 1
            enabled: root.configured && !root.notificationPolicyBusy
            onActivated: {
              var values = ["all", "messages", "none"]
              root.notificationPolicyBusy = true
              backendBridge.request("set_notification_policy", {
                policy: values[currentIndex]
              })
            }
          }
          FerryCheckBox {
            text: "Only notify for contacts"
            checked: root.contactsOnlyNotifications
            enabled: root.configured && root.notificationPolicy !== "none"
              && !root.contactsOnlyNotificationsBusy
            Accessible.description: "Unknown senders remain available in message history"
            onClicked: {
              root.contactsOnlyNotifications = checked
              root.contactsOnlyNotificationsBusy = true
              backendBridge.request("set_contacts_only_notifications", {
                enabled: checked
              })
            }
          }
          FerrySectionLabel {
            text: "Local data"
          }
          FerryComboBox {
            id: storagePolicyCombo
            Layout.fillWidth: true
            model: [
              "Encrypted with desktop keyring",
              "Unencrypted local data",
              "Do not retain local data"
            ]
            currentIndex: root.storagePolicy === "plaintext" ? 1
              : root.storagePolicy === "none" ? 2 : 0
            enabled: root.configured && !root.storagePolicyBusy
            onActivated: {
              var values = ["encrypted", "plaintext", "none"]
              if (values[currentIndex] !== root.storagePolicy
                  && !confirmStorageChange.checked) {
                root.errorText = "Confirm clearing local messages and contacts first"
                root.reload()
                return
              }
              if (values[currentIndex] === "encrypted")
                root.storageUnlockAttempted = true
              root.storagePolicyBusy = true
              backendBridge.request("set_storage_policy", {
                policy: values[currentIndex]
              })
              confirmStorageChange.checked = false
            }
          }
          FerryCheckBox {
            id: confirmStorageChange
            text: "I understand that changing storage mode clears local messages and contacts"
          }
              }
            }

            RowLayout {
              Layout.fillWidth: true
              FerryButton {
                text: "‹ MESSAGES"
                labelSize: theme.captionSize
                labelBold: true
                labelLetterSpacing: 1
                bare: true
                subtle: true
                leftPadding: theme.scaled(4)
                Accessible.name: "Back to messages"
                ToolTip.visible: hovered
                ToolTip.text: "Back to messages"
                onClicked: root.phoneSettingsVisible = false
              }
              Item { Layout.fillWidth: true }
            }
          }
        }
      }

      Popup {
        id: replaceTargetPopup
        parent: applicationSurface
        x: Math.max(0, (applicationSurface.width - width) / 2)
        y: Math.max(0, (applicationSurface.height - height) / 2)
        width: Math.min(theme.scaled(440), applicationSurface.width - theme.scaled(24))
        modal: true
        focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        padding: theme.scaled(18)

        background: Rectangle {
          color: theme.cardSurface
          border.color: theme.divider
          radius: theme.panelRadius
        }

        Overlay.modal: Rectangle {
          color: Qt.rgba(theme.windowSurface.r, theme.windowSurface.g,
                         theme.windowSurface.b, 0.72)
        }

        contentItem: ColumnLayout {
          spacing: theme.scaled(12)
          FerryLabel {
            text: "Replace the saved iPhone?"
            font.bold: true
            font.pixelSize: theme.headingSize
          }
          FerryLabel {
            Layout.fillWidth: true
            wrapMode: Text.Wrap
            text: "Pairing this iPhone will remove BlueFerry's saved phone and its local Bluetooth bond. Before continuing, also forget this computer in the old iPhone's Bluetooth settings."
          }
          RowLayout {
            Layout.alignment: Qt.AlignRight
            FerryButton {
              text: "Cancel"
              onClicked: replaceTargetPopup.close()
            }
            FerryButton {
              text: "Replace and pair"
              highlighted: true
              onClicked: {
                var device = root.pendingPairingDevice
                replaceTargetPopup.close()
                if (device !== null) root.startPairing(device, true)
              }
            }
          }
        }
      }

      Popup {
        id: pairingIssuePopup
        parent: applicationSurface
        x: Math.max(0, (applicationSurface.width - width) / 2)
        y: Math.max(0, (applicationSurface.height - height) / 2)
        width: Math.min(theme.scaled(440), applicationSurface.width - theme.scaled(24))
        modal: true
        focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        padding: theme.scaled(18)

        background: Rectangle {
          color: theme.cardSurface
          border.color: theme.divider
          radius: theme.panelRadius
        }

        Overlay.modal: Rectangle {
          color: Qt.rgba(theme.windowSurface.r, theme.windowSurface.g,
                         theme.windowSurface.b, 0.72)
        }

        contentItem: ColumnLayout {
          spacing: theme.scaled(12)
          FerryLabel {
            text: "Report pairing issue"
            font.bold: true
            font.pixelSize: theme.headingSize
          }
          FerryLabel {
            Layout.fillWidth: true
            wrapMode: Text.Wrap
            text: "A pairing report was saved at " + root.pairingIssueReport
              + ". Attach that file to a GitHub issue and include the iPhone model and iOS version."
          }
          RowLayout {
            Layout.alignment: Qt.AlignRight
            FerryButton {
              text: "Cancel"
              onClicked: pairingIssuePopup.close()
            }
            FerryButton {
              text: "Open GitHub"
              highlighted: true
              onClicked: {
                pairingIssuePopup.close()
                pairingIssueUrlProcess.running = true
              }
            }
          }
        }
      }

      Popup {
        id: deleteThreadsPopup
        property string threadKey: ""
        parent: applicationSurface
        x: Math.max(0, (applicationSurface.width - width) / 2)
        y: Math.max(0, (applicationSurface.height - height) / 2)
        width: Math.min(theme.scaled(440), applicationSurface.width - theme.scaled(24))
        modal: true
        focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        padding: theme.scaled(18)

        background: Rectangle {
          color: theme.cardSurface
          border.color: theme.divider
          radius: theme.panelRadius
        }

        Overlay.modal: Rectangle {
          color: Qt.rgba(theme.windowSurface.r, theme.windowSurface.g,
                         theme.windowSurface.b, 0.72)
        }

        contentItem: ColumnLayout {
          spacing: theme.scaled(12)
          FerryLabel {
            text: "Delete conversation?"
            font.bold: true
            font.pixelSize: theme.headingSize
          }
          FerryLabel {
            Layout.fillWidth: true
            wrapMode: Text.Wrap
            text: "This permanently deletes this local message history and group metadata. Nothing is deleted from your iPhone. A new message can create the conversation again."
          }
          RowLayout {
            Layout.alignment: Qt.AlignRight
            FerryButton {
              text: "Cancel"
              onClicked: deleteThreadsPopup.close()
            }
            FerryButton {
              text: root.deleteThreadsBusy ? "Deleting…" : "Delete locally"
              highlighted: true
              enabled: !root.deleteThreadsBusy
              onClicked: {
                root.deleteThreadsBusy = true
                backendBridge.request("delete_threads", {
                  thread_keys: [deleteThreadsPopup.threadKey]
                })
                deleteThreadsPopup.close()
              }
            }
          }
        }
      }

      Menu {
        id: threadContextMenu
        property string threadKey: ""

        MenuItem {
          text: {
            var thread = root.threadByKey(threadContextMenu.threadKey)
            return thread && thread.starred
              ? "Unstar Conversation" : "Star Conversation"
          }
          onTriggered: {
            var thread = root.threadByKey(threadContextMenu.threadKey)
            root.setThreadStarred(
              threadContextMenu.threadKey,
              !(thread && thread.starred)
            )
          }
        }
        MenuItem {
          text: "Delete Conversation"
          onTriggered: {
            deleteThreadsPopup.threadKey = threadContextMenu.threadKey
            deleteThreadsPopup.open()
          }
        }
      }

      Popup {
        id: newMessagePopup
        parent: applicationSurface
        x: Math.max(0, (applicationSurface.width - width) / 2)
        y: Math.max(0, (applicationSurface.height - height) / 2)
        width: Math.min(theme.scaled(440), applicationSurface.width - theme.scaled(24))
        modal: true
        focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        padding: theme.scaled(18)

        onOpened: {
          newRecipient.text = ""
          newMessageBody.text = ""
          root.searchContacts("")
          newRecipient.forceActiveFocus()
        }

        Timer {
          id: contactSearchTimer
          interval: 180
          onTriggered: root.searchContacts(newRecipient.text)
        }

        background: Rectangle {
          color: theme.cardSurface
          border.color: theme.divider
          radius: theme.panelRadius
        }

        Overlay.modal: Rectangle {
          color: Qt.rgba(theme.windowSurface.r, theme.windowSurface.g,
                         theme.windowSurface.b, 0.72)
        }

        contentItem: ColumnLayout {
          spacing: theme.scaled(8)

          FerryLabel {
            text: "New message"
            font.bold: true
            font.pixelSize: theme.headingSize
          }
          FerryLabel {
            text: "To"
            color: theme.muted
          }
          FerryTextField {
            id: newRecipient
            Layout.fillWidth: true
            placeholderText: "Contact, phone number, or email address"
            Accessible.name: "Recipient"
            onTextEdited: contactSearchTimer.restart()
          }
          ListView {
            id: newContactResults
            Layout.fillWidth: true
            Layout.preferredHeight: count > 0
              ? Math.min(contentHeight, theme.scaled(180)) : 0
            visible: count > 0
            clip: true
            model: root.contactResults
            delegate: ItemDelegate {
              id: newContactDelegate
              required property var modelData
              width: newContactResults.width
              implicitHeight: newContactText.implicitHeight + theme.scaled(12)
              contentItem: Column {
                id: newContactText
                Text {
                  text: newContactDelegate.modelData.name
                  textFormat: Text.PlainText
                  color: theme.windowText
                  font.family: theme.fontFamily
                  font.pixelSize: theme.baseFontSize
                }
                Text {
                  text: newContactDelegate.modelData.address.indexOf("@") >= 0
                    ? newContactDelegate.modelData.address
                    : "+" + newContactDelegate.modelData.address
                  textFormat: Text.PlainText
                  color: theme.muted
                  font.family: theme.fontFamily
                  font.pixelSize: theme.captionSize
                }
              }
              onClicked: {
                newRecipient.text = modelData.address
                root.searchContacts("")
                newMessageBody.forceActiveFocus()
              }
            }
          }
          FerryLabel {
            text: "Message"
            color: theme.muted
          }
          FerryMessageComposer {
            id: newMessageBody
            Layout.fillWidth: true
            placeholderText: "Write a message"
            Accessible.name: "Message text"
            onAccepted: {
              if (newMessageSendButton.enabled) newMessageSendButton.clicked()
            }
          }
          RowLayout {
            Layout.alignment: Qt.AlignRight
            FerryButton {
              text: "Cancel"
              onClicked: newMessagePopup.close()
            }
            FerryButton {
              id: newMessageSendButton
              text: root.newMessageSendBusy ? "Sending…" : "Send"
              highlighted: true
              enabled: newRecipient.text.trim() !== ""
                && newMessageBody.text.trim() !== ""
                && !root.newMessageSendBusy
              onClicked: {
                root.newMessageSendBusy = true
                backendBridge.request("send", {
                  recipient: newRecipient.text,
                  body: newMessageBody.text
                })
              }
            }
          }
        }
      }

      Popup {
        id: rosterChangedPopup
        parent: applicationSurface
        x: Math.max(0, (applicationSurface.width - width) / 2)
        y: Math.max(0, (applicationSurface.height - height) / 2)
        width: Math.min(theme.scaled(500), applicationSurface.width - theme.scaled(24))
        modal: true
        focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        padding: theme.scaled(18)

        background: Rectangle {
          color: theme.cardSurface
          border.color: theme.warning
          radius: theme.panelRadius
        }

        Overlay.modal: Rectangle {
          color: Qt.rgba(theme.windowSurface.r, theme.windowSurface.g,
                         theme.windowSurface.b, 0.72)
        }

        contentItem: ColumnLayout {
          spacing: theme.scaled(12)
          FerryLabel {
            text: "Group membership may have changed"
            font.bold: true
            font.pixelSize: theme.headingSize
          }
          FerryLabel {
            Layout.fillWidth: true
            wrapMode: Text.Wrap
            text: root.rosterChangedThread
              ? (root.rosterChangedThread.unexpected_sender || "Someone new")
                + " sent a message to " + root.rosterChangedThread.name
                + ", but is not in BlueFerry's saved participant list. Replies are disabled until you review the list. This can also happen if you have multiple groups named "
                + root.rosterChangedThread.name
                + ", because BlueFerry cannot distinguish them."
              : ""
          }
          RowLayout {
            Layout.alignment: Qt.AlignRight
            FerryButton {
              text: "Not now"
              onClicked: rosterChangedPopup.close()
            }
            FerryButton {
              text: "Review participants"
              highlighted: true
              onClicked: {
                root.groupParticipantsThread = root.rosterChangedThread
                rosterChangedPopup.close()
                groupParticipantsPopup.open()
              }
            }
          }
        }
      }

      Popup {
        id: groupParticipantsPopup
        parent: applicationSurface
        x: Math.max(0, (applicationSurface.width - width) / 2)
        y: Math.max(0, (applicationSurface.height - height) / 2)
        width: Math.min(theme.scaled(520), applicationSurface.width - theme.scaled(24))
        modal: true
        focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        padding: theme.scaled(18)

        onOpened: {
          groupParticipantsEditor.text = root.groupParticipantsThread
            ? (root.groupParticipantsThread.recipients || []).join("\n") : ""
          groupParticipantsEditor.forceActiveFocus()
        }

        background: Rectangle {
          color: theme.cardSurface
          border.color: theme.divider
          radius: theme.panelRadius
        }

        Overlay.modal: Rectangle {
          color: Qt.rgba(theme.windowSurface.r, theme.windowSurface.g,
                         theme.windowSurface.b, 0.72)
        }

        contentItem: ColumnLayout {
          spacing: theme.scaled(10)

          FerryLabel {
            text: root.groupParticipantsThread
              ? "Who is in " + root.groupParticipantsThread.name + "?" : "Group participants"
            font.bold: true
            font.pixelSize: theme.headingSize
          }
          FerryLabel {
            Layout.fillWidth: true
            wrapMode: Text.Wrap
            text: root.groupParticipantsThread
              ? (root.groupParticipantsThread.prompt_sender || "Someone")
                + " has sent a message to a group named "
                + root.groupParticipantsThread.name
                + ", which you're a member of. BlueFerry can't determine the participants of this group chat, but if you fill in the members, it can work."
              : ""
          }
          FerryLabel {
            Layout.fillWidth: true
            wrapMode: Text.Wrap
            color: theme.muted
            text: "Enter every other participant's phone number or Apple ID email, one per line."
          }
          FerryLabel {
            Layout.fillWidth: true
            wrapMode: Text.Wrap
            color: theme.windowText
            text: "Changing this list only updates BlueFerry's local understanding of the group. It does not add or remove anyone in Messages on your iPhone."
          }
          Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: theme.scaled(150)
            color: theme.control
            border.color: groupParticipantsEditor.activeFocus
              ? theme.accent : theme.divider
            radius: theme.controlRadius
            ScrollView {
              anchors.fill: parent
              anchors.margins: theme.scaled(2)
              TextArea {
                id: groupParticipantsEditor
                color: theme.windowText
                selectionColor: theme.accent
                selectedTextColor: theme.highlightedText
                font.family: theme.fontFamily
                font.pixelSize: theme.baseFontSize
                placeholderText: "One participant per line"
                placeholderTextColor: theme.muted
                wrapMode: TextEdit.NoWrap
                background: null
                Accessible.name: "Group participants"
              }
            }
          }
          FerryLabel {
            Layout.fillWidth: true
            wrapMode: Text.Wrap
            color: theme.warning
            text: root.groupParticipantsThread
              ? "BlueFerry identifies named groups by name. If you have multiple groups named "
                + root.groupParticipantsThread.name
                + ", BlueFerry may combine them and use the wrong participant list. This list can also become outdated if the group is renamed or its membership changes."
              : ""
          }
          RowLayout {
            Layout.alignment: Qt.AlignRight
            FerryButton {
              text: "Cancel"
              onClicked: groupParticipantsPopup.close()
            }
            FerryButton {
              text: root.groupParticipantsBusy ? "Saving…" : "Save participants"
              highlighted: true
              enabled: root.participantLines(groupParticipantsEditor.text).length >= 2
                && !root.groupParticipantsBusy
              onClicked: {
                var recipients = root.participantLines(groupParticipantsEditor.text)
                root.groupParticipantsBusy = true
                backendBridge.request("set_group_participants", {
                  thread_key: root.groupParticipantsThread.key,
                  recipients: recipients
                })
              }
            }
          }
        }
      }
    }
  }

  component FerryLabel: Label {
    color: theme.windowText
    textFormat: Text.PlainText
    font.family: theme.fontFamily
    font.pixelSize: theme.baseFontSize
  }

  component FerryButton: Button {
    id: control
    property real labelSize: theme.baseFontSize
    property bool bare: false
    property bool subtle: false
    property bool labelBold: false
    property real labelLetterSpacing: 0
    implicitHeight: theme.scaled(34)
    leftPadding: theme.scaled(12)
    rightPadding: theme.scaled(12)
    topPadding: theme.scaled(7)
    bottomPadding: theme.scaled(7)

    contentItem: Text {
      text: control.text
      textFormat: Text.PlainText
      color: !control.enabled ? theme.muted
        : control.bare && control.hovered ? theme.accent
        : control.highlighted ? theme.primaryText
        : control.subtle ? theme.muted : theme.windowText
      font.family: theme.fontFamily
      font.pixelSize: control.labelSize
      font.bold: control.labelBold || control.highlighted
      font.letterSpacing: control.labelLetterSpacing
      horizontalAlignment: Text.AlignHCenter
      verticalAlignment: Text.AlignVCenter
      elide: Text.ElideRight
    }
    background: Rectangle {
      color: control.bare ? "transparent"
        : control.highlighted
        ? theme.primarySurface
        : control.down || control.checked ? theme.selectedSurface
          : control.hovered ? theme.hoverSurface : theme.control
      border.color: control.bare
        ? control.activeFocus ? theme.accent : "transparent"
        : control.activeFocus ? theme.accent
        : control.highlighted ? "transparent" : theme.divider
      radius: theme.controlRadius
      opacity: control.enabled ? 1.0 : 0.55
    }
  }

  component FerryTextField: TextField {
    id: control
    property bool flat: false
    implicitHeight: theme.scaled(36)
    leftPadding: theme.scaled(10)
    rightPadding: theme.scaled(10)
    color: theme.windowText
    placeholderTextColor: theme.muted
    selectionColor: theme.accent
    selectedTextColor: theme.highlightedText
    font.family: theme.fontFamily
    font.pixelSize: theme.baseFontSize
    selectByMouse: true
    background: Rectangle {
      color: control.flat ? "transparent" : theme.control
      border.color: control.activeFocus ? theme.accent
        : control.flat ? "transparent" : theme.divider
      radius: theme.controlRadius
    }
  }

  component FerryMessageComposer: ScrollView {
    id: control
    property alias text: editor.text
    property alias placeholderText: editor.placeholderText
    property bool flat: false
    signal accepted()

    Layout.minimumWidth: 0
    Layout.minimumHeight: theme.scaled(36)
    Layout.preferredHeight: Math.min(
      Math.max(editor.contentHeight + editor.topPadding + editor.bottomPadding + 2,
               Layout.minimumHeight),
      Layout.maximumHeight
    )
    Layout.maximumHeight: theme.scaled(144)
    clip: true
    ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
    ScrollBar.vertical.policy: ScrollBar.AsNeeded

    function clear() { editor.clear() }
    function forceActiveFocus() { editor.forceActiveFocus() }
    function submit(event) {
      if ((event.modifiers & Qt.ShiftModifier) !== 0) {
        event.accepted = false
        return
      }
      accepted()
      event.accepted = true
    }

    background: Rectangle {
      color: control.flat ? "transparent" : theme.control
      border.color: editor.activeFocus ? theme.accent
        : control.flat ? "transparent" : theme.divider
      radius: theme.controlRadius
    }

    TextArea {
      id: editor
      width: control.availableWidth
      leftPadding: theme.scaled(10)
      rightPadding: theme.scaled(10)
      color: theme.windowText
      placeholderTextColor: theme.muted
      selectionColor: theme.accent
      selectedTextColor: theme.highlightedText
      font.family: theme.fontFamily
      font.pixelSize: theme.baseFontSize
      selectByMouse: true
      wrapMode: TextEdit.Wrap
      background: null
      Accessible.name: control.Accessible.name
      Keys.onReturnPressed: event => control.submit(event)
      Keys.onEnterPressed: event => control.submit(event)
    }
  }

  component FerryCheckBox: CheckBox {
    id: control
    spacing: theme.scaled(8)
    implicitHeight: Math.max(theme.scaled(24), contentItem.implicitHeight)
    indicator: Rectangle {
      x: control.leftPadding
      y: (control.height - height) / 2
      implicitWidth: theme.scaled(15)
      implicitHeight: theme.scaled(15)
      color: control.checked ? theme.primarySurface : theme.control
      border.color: control.activeFocus ? theme.accent : theme.divider
      radius: theme.scaled(4)
      Text {
        anchors.centerIn: parent
        text: control.checked ? "✓" : ""
        color: control.checked ? theme.primaryText : theme.windowText
        font.family: theme.fontFamily
        font.pixelSize: theme.captionSize
      }
    }
    contentItem: FerryLabel {
      leftPadding: control.indicator.width + control.spacing
      text: control.text
      color: control.enabled ? theme.windowText : theme.muted
      font.family: theme.fontFamily
      font.pixelSize: theme.bodySmallSize
      wrapMode: Text.Wrap
      verticalAlignment: Text.AlignVCenter
    }
  }

  component FerryComboBox: ComboBox {
    id: control
    implicitHeight: theme.scaled(36)
    leftPadding: theme.scaled(10)
    rightPadding: theme.scaled(30)
    font.family: theme.fontFamily
    font.pixelSize: theme.baseFontSize

    contentItem: FerryLabel {
      text: control.displayText
      color: control.enabled ? theme.windowText : theme.muted
      font: control.font
      verticalAlignment: Text.AlignVCenter
      elide: Text.ElideRight
    }
    indicator: Text {
      x: control.width - width - theme.scaled(10)
      y: (control.height - height) / 2
      text: "⌄"
      color: theme.muted
      font.family: theme.fontFamily
      font.pixelSize: theme.baseFontSize
    }
    background: Rectangle {
      color: control.hovered ? theme.hoverSurface : theme.control
      border.color: control.activeFocus ? theme.accent : theme.divider
      radius: theme.controlRadius
    }
    delegate: ItemDelegate {
      id: option
      required property var modelData
      required property int index
      width: control.width
      highlighted: control.highlightedIndex === index
      contentItem: Text {
        text: option.modelData
        textFormat: Text.PlainText
        color: theme.windowText
        font: control.font
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
      }
      background: Rectangle {
        color: option.highlighted ? theme.selectedSurface
          : option.hovered ? theme.hoverSurface : theme.windowSurface
      }
    }
    popup: Popup {
      y: control.height + 1
      width: control.width
      implicitHeight: Math.min(contentItem.implicitHeight, theme.scaled(240))
      padding: 1
      contentItem: ListView {
        clip: true
        implicitHeight: contentHeight
        model: control.popup.visible ? control.delegateModel : null
        currentIndex: control.highlightedIndex
        ScrollIndicator.vertical: ScrollIndicator { }
      }
      background: Rectangle {
        color: theme.windowSurface
        border.color: theme.divider
        radius: theme.controlRadius
      }
    }
  }

  component FerrySectionLabel: FerryLabel {
    color: theme.muted
    font.family: theme.fontFamily
    font.pixelSize: theme.captionSize
    font.bold: true
    font.capitalization: Font.AllUppercase
    font.letterSpacing: 1
    topPadding: theme.scaled(12)
    bottomPadding: theme.scaled(2)
  }

  component FerryInfoRow: Item {
    property string label: ""
    property string value: ""
    implicitHeight: Math.max(infoLabel.implicitHeight, infoValue.implicitHeight)
      + theme.scaled(18)
    Rectangle {
      anchors.fill: parent
      color: theme.cardSurface
      border.color: theme.divider
      radius: theme.controlRadius
    }
    Text {
      id: infoLabel
      anchors.left: parent.left
      anchors.leftMargin: theme.scaled(12)
      anchors.verticalCenter: parent.verticalCenter
      text: parent.label
      color: theme.muted
      font.family: theme.fontFamily
      font.pixelSize: theme.bodySmallSize
    }
    Text {
      id: infoValue
      anchors.right: parent.right
      anchors.rightMargin: theme.scaled(12)
      anchors.verticalCenter: parent.verticalCenter
      text: parent.value
      color: theme.windowText
      font.family: theme.fontFamily
      font.pixelSize: theme.bodySmallSize
    }
  }
}
