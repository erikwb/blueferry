import QtQuick

// Shared by the Qt and Quickshell adapters. Window visibility, navigation,
// transport calls, and dialogs stay with their respective UI.
QtObject {
    property var warnedRosterChanges: ({})
    readonly property int maxParticipants: 20

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
        return !!thread && thread.unread === true
    }

    function groupSignature(thread) {
        return thread && thread.is_group ? String(thread.confirmation_token || "") : ""
    }

    function nextRosterWarning(threads) {
        for (let index = 0; index < threads.length; ++index) {
            const thread = threads[index]
            if (!thread.roster_changed) continue
            const warningId = thread.roster_warning_key
            if (warnedRosterChanges[warningId] === true) continue
            warnedRosterChanges[warningId] = true
            return thread
        }
        return null
    }

    // A group whose members are unconfirmed can be written to; Send then
    // asks for its members (conversation_state.NEEDS_PARTICIPANTS). So can a
    // conversation whose latest message may be from a group; Send then asks
    // whether to reply to the sender only (CONFIRM_SENDER).
    function canCompose(thread) {
        return !!thread && (thread.reply_ready === true
            || (thread.is_group === true && thread.participants_required === true)
            || needsSenderConfirmation(thread))
    }

    function needsSenderConfirmation(thread) {
        return !!thread && thread.is_group !== true && thread.reply_ready !== true
            && String(thread.sender_reply_token || "") !== ""
    }

    // The members editor, like conversation_state.ParticipantDraft. Entries
    // are {name, address}; the backend validates the list that is saved.
    function participantIdentity(address) {
        const text = String(address || "").trim()
        return text.indexOf("@") >= 0 ? text.toLowerCase() : text.replace(/[^0-9]/g, "")
    }

    function participantAddress(value) {
        const text = String(value || "").trim()
        return /^\+?[0-9][0-9 ()\-.]{2,29}$/.test(text)
            || /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(text) ? text : ""
    }

    function appendParticipant(entries, name, address) {
        const identity = participantIdentity(address)
        for (let index = 0; index < entries.length; ++index) {
            if (participantIdentity(entries[index].address) === identity) return entries
        }
        return entries.concat([{name: String(name || ""), address: String(address)}])
    }

    function participantDraft(thread) {
        let entries = []
        const draft = thread && thread.participant_draft ? thread.participant_draft : []
        for (let index = 0; index < draft.length; ++index) {
            const address = String(draft[index].address || "").trim()
            if (address !== "") entries = appendParticipant(entries, draft[index].name, address)
        }
        return entries
    }

    // The new list, or null when the value is not a phone number or email
    // address or the list is full.
    function addParticipant(entries, value, name) {
        const address = participantAddress(value)
        if (address === "" || entries.length >= maxParticipants) return null
        return appendParticipant(entries, name, address)
    }

    function removeParticipant(entries, address) {
        return entries.filter(entry => entry.address !== address)
    }

    function missingMembers(thread, entries) {
        const named = {}
        for (let index = 0; index < entries.length; ++index) {
            if (entries[index].name) named[entries[index].name.toLowerCase()] = true
        }
        const members = thread && thread.missing_members ? thread.missing_members : []
        const missing = []
        for (let index = 0; index < members.length; ++index) {
            const member = String(members[index])
            if (named[member.toLowerCase()] !== true) missing.push(member)
        }
        return missing
    }

    function participantSuggestions(results, entries) {
        const present = {}
        for (let index = 0; index < entries.length; ++index) {
            present[participantIdentity(entries[index].address)] = true
        }
        const suggestions = []
        for (let index = 0; index < (results || []).length; ++index) {
            if (present[participantIdentity(results[index].address)] !== true)
                suggestions.push(results[index])
        }
        return suggestions
    }
}
