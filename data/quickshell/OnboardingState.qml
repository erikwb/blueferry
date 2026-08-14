import QtQuick

QtObject {
  required property bool notificationsSupported
  required property bool bluezActive
  required property bool configured
  required property var backendStatus

  readonly property string stage: {
    if (notificationsSupported && !bluezActive) return "activate-bluetooth"
    if (!configured) return "select-device"
    if (backendStatus.map && backendStatus.pbap) {
      if (pendingIphoneSetupTasks().length > 0) return "iphone-settings"
      return notificationsSupported ? "ready" : "ready-without-ancs"
    }
    return backendStatus.daemon ? "iphone-settings" : "starting"
  }

  function mapConnectionRefused() {
    return backendStatus.map_connection_refused === true
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
    var tasks = pendingIphoneSetupTasks().join("\n• ")
    var instructions = "After approving “Allow System Notifications,” you may need to return to the Bluetooth device list and reopen this computer before the other settings appear."
    var text = configured
      ? instructions + "\n• " + tasks
      : "On the iPhone open Settings → Bluetooth, tap ⓘ next to this computer, then finish the settings below. " + instructions + "\n• " + tasks
    var verified = backendStatus.verified_iphone_setup || []
    if (notificationsSupported && verified.indexOf("notification-access") < 0)
      text += "\n\nWithout System Notification access, group texts appear as individual conversations with their sender."
    return text
  }
}
