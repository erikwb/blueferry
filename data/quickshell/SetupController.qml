import QtQuick

// Pure setup state. The transport supplies output tagged with the request ID;
// the view never owns a Process or parses helper output.
QtObject {
  id: setup

  signal executeRequested(int requestId, string kind, var command, bool interactive)
  signal cancelRequested(int requestId)
  signal inputRequested(int requestId, string text)
  signal configurationUpdated(bool configured)
  signal reloadRequested()
  signal historyReset()
  signal confirmationRequested()
  signal replacementRequested()
  signal issueUrlReady(string url)

  property var pending: ({})
  property int nextRequestId: 1
  property var pairingDevices: []
  property int selectedDeviceIndex: -1
  property string pairingStatus: ""
  property string configurationError: ""
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
  property string pairingIssueReport: ""
  property string adapterName: ""
  property var adapters: []
  property var pendingReplacement: null

  readonly property bool scanning: pending.devices !== undefined
  readonly property bool pairing: pending.pair !== undefined
  readonly property bool forgetting: pending.forget !== undefined
  readonly property bool activating: pending.activate !== undefined
  readonly property bool changingPhone: pairing || forgetting
  readonly property bool canPair: selectedPairingDevice() !== null
    && compatibilityLoaded && !scanning && !changingPhone && !activating

  function selectedPairingDevice() {
    return selectedDeviceIndex >= 0 && selectedDeviceIndex < pairingDevices.length
      ? pairingDevices[selectedDeviceIndex] : null
  }

  function configuredPairingDevice() {
    return pairingDevices.find(device => device.mac === configuredMac) || null
  }

  function cancel(kind) {
    const request = pending[kind]
    if (!request) return
    const next = Object.assign({}, pending)
    delete next[kind]
    pending = next
    cancelRequested(request.id)
  }

  function request(kind, command, interactive, context) {
    cancel(kind)
    const id = nextRequestId++
    pending = Object.assign({}, pending, {
      [kind]: {id: id, context: context || {}}
    })
    executeRequested(id, kind, command, interactive === true)
  }

  function start() {
    refreshConfiguration()
    loadCompatibility("")
  }

  function refreshConfiguration() {
    if (changingPhone || pending.configuration) return
    request("configuration", ["/usr/bin/blueferry", "pairing-configuration-json"])
  }

  function loadCompatibility(adapter) {
    if (changingPhone || activating) return
    cancel("devices")
    pairingDevices = []
    selectedDeviceIndex = -1
    adapterName = adapter || ""
    compatibilityLoaded = false
    const command = ["/usr/bin/blueferry", "pairing-compatibility-json"]
    if (adapterName) command.push("--adapter", adapterName)
    request("compatibility", command)
  }

  function loadDevices(scan) {
    if (changingPhone || activating || !compatibilityLoaded) return
    const command = ["/usr/bin/blueferry", "pairing-devices-json"]
    if (scan) command.push("--scan-seconds", "24")
    if (adapterName) command.push("--adapter", adapterName)
    if (scan) pairingStatus = "Scanning for Bluetooth devices…"
    request("devices", command, false, {adapter: adapterName, scan: scan === true})
  }

  function cancelScan() {
    cancel("devices")
    pairingStatus = "Scan cancelled. You can scan again."
  }

  function activateBluetooth() {
    if (changingPhone || activating) return
    cancel("compatibility")
    cancel("devices")
    pairingStatus = "Authorizing Bluetooth restart…"
    request("activate", ["/usr/bin/blueferry", "pairing-activate-bluez"])
  }

  function requestPairing() {
    if (!canPair) return
    const device = selectedPairingDevice()
    const command = ["/usr/bin/blueferry", "pairing-complete", device.mac, "--interactive-agent"]
    const adapter = device.adapter_path
      ? String(device.adapter_path).split("/").pop() : adapterName
    if (adapter) command.push("--adapter", adapter)
    if (!notificationsSupported || compatibilityModeOverride) command.push("--compatibility-mode")
    if (explicitPairingOverride) command.push("--explicit-pairing")
    if (!device.paired && targetSaved) {
      command.push("--replace-saved-mac", configuredMac)
      pendingReplacement = {command: command, adapter: adapter}
      replacementRequested()
    } else {
      beginPairing(command, adapter)
    }
  }

  function confirmReplacement() {
    const selected = pendingReplacement
    pendingReplacement = null
    if (selected && !changingPhone && !activating)
      beginPairing(selected.command, selected.adapter)
  }

  function beginPairing(command, adapter) {
    cancel("configuration")
    cancel("devices")
    cancel("compatibility")
    pairingStatus = "Starting secure pairing…"
    pairingIssueReport = ""
    clearConfirmation()
    request("pair", command, true, {adapter: adapter})
  }

  function forgetPhone() {
    if (!configuredMac || changingPhone || activating) return
    cancel("configuration")
    cancel("devices")
    cancel("compatibility")
    const command = ["/usr/bin/blueferry", "pairing-forget", configuredMac, "--interactive-approval"]
    if (configuredAdapter) command.push("--adapter", configuredAdapter)
    clearConfirmation()
    pairingStatus = "Requesting approval to unpair…"
    request("forget", command, true)
  }

  function answerConfirmation(accepted) {
    const kind = pairingConfirmationPurpose === "forget" ? "forget" : "pair"
    const active = pending[kind]
    if (!pairingConfirmationPending || !active) return
    clearConfirmation()
    pairingStatus = accepted ? "Finishing Bluetooth setup…" : "Cancelling…"
    inputRequested(active.id, accepted ? "yes\n" : "no\n")
  }

  function clearConfirmation() {
    pairingConfirmationPending = false
    pairingConfirmationPurpose = ""
    pairingPasskey = ""
  }

  function receiveLine(id, kind, line) {
    if (!pending[kind] || pending[kind].id !== id || (kind !== "pair" && kind !== "forget")) return
    try {
      const data = JSON.parse(line)
      if (data.event === "display") {
        pairingPasskey = String(data.passkey || "")
        pairingStatus = "Compare this code with the code on your iPhone."
      } else if (data.event === "confirmation") {
        pairingPasskey = String(data.passkey || "")
        pairingConfirmationPurpose = kind
        pairingConfirmationPending = true
        pairingStatus = kind === "forget" ? "Confirm unpairing this iPhone."
          : pairingPasskey ? "Check that the codes match on your iPhone."
            : "Approve only if you started this pairing."
        confirmationRequested()
      }
    } catch (error) { /* The terminal result is validated in finish(). */ }
  }

  function fail(kind, message) {
    if (kind === "configuration") {
      configurationError = message
      configurationUpdated(false)
      return
    }
    pairingStatus = message
    if (kind === "compatibility") {
      compatibilityLoaded = true
      hardwareSupported = false
      notificationsSupported = false
      ancsLimitedController = false
      controllerVendor = ""
      bluezActive = false
    }
  }

  function finish(id, kind, code, output, diagnostic) {
    const active = pending[kind]
    if (!active || active.id !== id) return
    const next = Object.assign({}, pending)
    delete next[kind]
    pending = next
    if (kind === "pair" || kind === "forget") clearConfirmation()
    try {
      const lines = String(output || "").trim().split("\n")
      if (kind === "issue" && code === 0) {
        const url = lines[lines.length - 1]
        if (!url.startsWith("https://")) throw new Error("Invalid issue URL")
        issueUrlReady(url)
        return
      }
      if (!String(output || "").trim())
        throw new Error(diagnostic || "Setup helper returned no result; try again.")
      const data = JSON.parse(lines[lines.length - 1])
      if (kind === "pair" && data)
        pairingIssueReport = String(data.quirks_report || data.report_path || "")
      if (code !== 0 || !data || data.error || data.ok === false)
        throw new Error(data && data.error || diagnostic || "Setup did not complete; try again.")
      if (kind === "compatibility") {
        if (typeof data.notifications_supported !== "boolean") throw new Error("Invalid compatibility response")
        hardwareSupported = data.hardware_supported === true
        notificationsSupported = data.notifications_supported === true
        ancsLimitedController = data.ancs_limited_controller === true
        controllerVendor = String(data.controller_vendor || "")
        bluezActive = data.bearer_api_active === true
        adapterName = String(data.adapter || adapterName)
        adapters = Array.isArray(data.adapters) ? data.adapters : []
        compatibilityLoaded = true
        loadDevices(false)
      } else if (kind === "configuration") {
        if (typeof data.configured !== "boolean") throw new Error("Invalid saved-phone response")
        configurationError = ""
        configured = data.configured
        targetSaved = data.saved === true
        bondStateKnown = typeof data.bonded === "boolean"
        targetBonded = data.bonded === true
        configuredMac = targetSaved ? String(data.mac || "") : ""
        configuredAdapter = targetSaved ? String(data.adapter || "") : ""
        ancsEnabled = data.ancs_enabled !== false
        if (data.pairing_issue_report) pairingIssueReport = String(data.pairing_issue_report)
        if (targetSaved && bondStateKnown && !targetBonded)
          pairingStatus = "This phone is no longer paired. Clear the saved phone, then pair again."
        configurationUpdated(configured)
      } else if (kind === "devices") {
        if (!Array.isArray(data)) throw new Error("Invalid Bluetooth scan response")
        const selected = selectedPairingDevice()
        const mac = selected ? selected.mac : ""
        const adapter = active.context.adapter
        pairingDevices = data.filter(device => device && typeof device.mac === "string"
          && (!adapter || String(device.adapter_path || "").endsWith("/" + adapter)))
          .map(device => Object.assign({}, device, {label: String(device.name || "iPhone") + " — " + device.mac
            + (device.paired ? " (paired)" : "")}))
        const previousIndex = pairingDevices.findIndex(device => device.mac === mac)
        selectedDeviceIndex = previousIndex >= 0 ? previousIndex : pairingDevices.length ? 0 : -1
        if (active.context.scan) pairingStatus = pairingDevices.length ? "Select your iPhone, then pair."
          : "No devices found. Keep the iPhone Bluetooth settings open and scan again."
      } else if (kind === "activate") {
        if (data.ok !== true) throw new Error("Bluetooth activation did not complete")
        bluezActive = true
        pairingStatus = "Bluetooth is ready. Scan for your iPhone."
        loadCompatibility(adapterName)
      } else if (kind === "pair") {
        if (data.ok !== true || !data.device || !data.device.mac) throw new Error("Invalid pairing result")
        configured = true
        targetSaved = true
        targetBonded = true
        bondStateKnown = true
        configuredMac = String(data.device.mac)
        configuredAdapter = data.device.adapter_path
          ? String(data.device.adapter_path).split("/").pop() : active.context.adapter
        ancsEnabled = data.ancs_enabled !== false
        pairingIssueReport = String(data.quirks_report || data.report_path || "")
        pairingStatus = "Paired. Enable Show Message Notifications and Sync Contacts on the iPhone."
        loadDevices(false)
        reloadRequested()
      } else if (kind === "forget") {
        if (data.ok !== true) throw new Error("Unpairing did not complete")
        configured = targetSaved = targetBonded = false
        bondStateKnown = true
        configuredMac = configuredAdapter = ""
        ancsEnabled = true
        pairingIssueReport = ""
        pairingDevices = []
        selectedDeviceIndex = -1
        historyReset()
        loadDevices(false)
        pairingStatus = "Local bond removed. Also forget this computer on the iPhone."
      }
    } catch (error) {
      fail(kind, String(error.message || error))
    }
    if (kind === "pair" || kind === "forget") refreshConfiguration()
  }

  function openPairingIssue() {
    if (!pending.issue) request("issue", ["/usr/bin/blueferry", "pairing-issue", "--print-url"])
  }
}
