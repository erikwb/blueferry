import QtQuick

// Shared by the Qt and Quickshell adapters. Window visibility, navigation,
// transport calls, and dialogs stay with their respective UI.
QtObject {
    property var warnedRosterChanges: ({})

    function threadByKey(threads, key) {
        for (let index = 0; index < threads.length; ++index) {
            if (threads[index].key === key
                || (threads[index].aliases || []).indexOf(key) >= 0) return threads[index]
        }
        return null
    }

    function threadForMessage(threads, handle) {
        for (let index = 0; index < threads.length; ++index) {
            const messages = threads[index].messages || []
            for (let messageIndex = 0; messageIndex < messages.length; ++messageIndex) {
                if (messages[messageIndex].handle === handle) return threads[index]
            }
        }
        return null
    }

    function threadIsUnread(thread) {
        if (!thread) return false
        if (thread.unread === true || thread.unread === false) return thread.unread
        const messages = thread.messages || []
        for (let index = 0; index < messages.length; ++index) {
            if (!messages[index].outgoing && messages[index].read === false) return true
        }
        return false
    }

    function groupSignature(thread) {
        if (!thread || !thread.is_group) return ""
        const unique = []
        const recipients = thread.recipients || []
        for (let index = 0; index < recipients.length; ++index) {
            const address = String(recipients[index] || "")
            if (address !== "" && unique.indexOf(address) < 0) unique.push(address)
        }
        unique.sort()
        return [String(thread.roster_warning_id || "")].concat(unique).join("\n")
    }

    function nextRosterWarning(threads) {
        for (let index = 0; index < threads.length; ++index) {
            const thread = threads[index]
            if (!thread.roster_changed) continue
            const warningId = thread.roster_warning_id
                || thread.key + ":" + (thread.unexpected_sender || "unknown")
            if (warnedRosterChanges[warningId] === true) continue
            warnedRosterChanges[warningId] = true
            return thread
        }
        return null
    }

    function participantLines(value) {
        const result = []
        // Match Python str.splitlines(), used by the GTK and terminal editors.
        const lines = value.split(/\r\n|[\n\r\v\f\u001c-\u001e\u0085\u2028\u2029]/)
        for (let index = 0; index < lines.length; ++index) {
            const address = lines[index].trim()
            if (address !== "" && result.indexOf(address) < 0) result.push(address)
        }
        return result
    }
}
