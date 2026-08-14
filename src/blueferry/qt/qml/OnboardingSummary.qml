import QtQuick
import org.kde.kirigami as Kirigami

Kirigami.InlineMessage {
    id: root

    required property string stage
    required property var compatibility
    required property var status

    visible: true
    text: htmlEscape(titleForStage()) + "\n" + htmlEscape(detailForStage())
    type: stage === "ready" || stage === "ready-without-ancs"
        ? Kirigami.MessageType.Positive
        : Kirigami.MessageType.Information

    function htmlEscape(value) {
        return String(value || "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
    }

    function pendingTasks() {
        const verified = status.verified_iphone_setup || []
        const tasks = []
        if (verified.indexOf("message-notifications") < 0)
            tasks.push(qsTr("Enable Show Message Notifications"))
        if (verified.indexOf("contacts") < 0)
            tasks.push(qsTr("Enable Sync Contacts"))
        if (compatibility.notifications_supported
                && verified.indexOf("notification-access") < 0)
            tasks.push(qsTr("Allow Notification Access when prompted"))
        return tasks
    }

    function pendingTasksText() {
        let detail = qsTr("Open Settings → Bluetooth on the iPhone, tap ⓘ next to this computer, then finish the settings below. After approving “Allow System Notifications,” you may need to return to the Bluetooth device list and reopen this computer before the other settings appear:\n• ")
            + pendingTasks().join("\n• ")
        const verified = status.verified_iphone_setup || []
        if (compatibility.notifications_supported
                && verified.indexOf("notification-access") < 0)
            detail += qsTr("\n\nWithout System Notification access, group texts appear as individual conversations with their sender.")
        return detail
    }

    function titleForStage() {
        const titles = {
            "checking": qsTr("Checking Bluetooth Support"),
            "activate-bluetooth": qsTr("Activate Bluetooth Support"),
            "select-device": qsTr("Pair an iPhone"),
            "starting": qsTr("Starting the Background Service"),
            "iphone-settings": qsTr("Finish Setup on the iPhone"),
            "ready": qsTr("BlueFerry Is Connected"),
            "ready-without-ancs": qsTr("Messages Are Connected")
        }
        return titles[stage] || qsTr("Set Up BlueFerry")
    }

    function detailForStage() {
        const details = {
            "checking": qsTr("Inspecting the selected Bluetooth controller without changing it."),
            "activate-bluetooth": qsTr("The packaged BlueZ bearer support needs one authorized Bluetooth restart."),
            "select-device": qsTr("Scan for and select your iPhone here, then choose Pair. When the pairing request appears on the iPhone, approve it and confirm that the codes match. Pairing may appear idle for up to 15 seconds. After it completes, return to the Bluetooth device list and open this computer's ⓘ page a few times; turn on any new toggles that appear. System Notification access is also how BlueFerry recognizes group text threads; without it, a group text appears as a one-to-one conversation with its sender."),
            "starting": qsTr("The configured backend is starting. This normally takes a few seconds."),
            "iphone-settings": pendingTasksText(),
            "ready": qsTr("Bluetooth services and iPhone permissions have been verified."),
            "ready-without-ancs": qsTr("Messages and contacts have been verified. System notifications are unavailable, so group texts may appear as individual conversations.")
        }
        return details[stage] || ""
    }
}
