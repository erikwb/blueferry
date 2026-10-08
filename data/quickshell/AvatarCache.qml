pragma ComponentBehavior: Bound

import QtQuick

Item {
  id: photos
  visible: false
  required property var bridge
  property var backendStatus: ({})
  property bool photosEnabled: false
  property var contactRevision: undefined
  property int generation: 0
  property int revision: 0
  property var sources: ({})
  property var pending: ({})
  property var tickets: ({})
  property var retryAt: ({})
  property var failures: ({})
  property int cachedChars: 0
  property int maxEntries: 512
  property int maxChars: 16 * 1024 * 1024
  property int maxPending: 8

  onBackendStatusChanged: {
    const active = backendStatus && backendStatus.contact_photos === true
    const current = backendStatus ? backendStatus.contact_photo_revision : undefined
    if (active !== photosEnabled || current !== contactRevision) {
      // Changing a property can immediately re-evaluate an image binding.
      // Retire the old generation before allowing it to enqueue new reads.
      photosEnabled = false
      generation++
      sources = ({})
      retryAt = ({})
      failures = ({})
      cachedChars = 0
      contactRevision = current
      photosEnabled = active
    }
    revision++
  }

  function source(address) {
    if (!photosEnabled || !address) return ""
    if (Object.prototype.hasOwnProperty.call(sources, address)) return sources[address]
    if (pending[address] !== undefined || Object.keys(tickets).length >= maxPending
        || Date.now() < (retryAt[address] || 0)) return ""
    if (retryAt[address] === undefined
        && Object.keys(sources).length + Object.keys(pending).length
          + Object.keys(retryAt).length >= maxEntries) return ""
    delete retryAt[address]
    const id = bridge.request("contact_photo", {address: String(address)})
    tickets[id] = {address: address, generation: generation}
    pending[address] = id
    return ""
  }

  function takeTicket(id) {
    const ticket = tickets[id]
    if (!ticket) return null
    delete tickets[id]
    delete pending[ticket.address]
    return ticket
  }

  function accept(id, result) {
    const ticket = takeTicket(id)
    if (!ticket) return
    if (ticket.generation === generation && photosEnabled) {
      if (!result || result.address !== ticket.address || typeof result.source !== "string"
          || result.source.length > 1398200
          || (result.source !== "" && !result.source.startsWith("data:image/png;base64,")
            && !result.source.startsWith("data:image/jpeg;base64,"))) {
        retry(ticket)
      } else {
        // When full, cache the fallback instead of evicting a visible avatar
        // and repeatedly fetching it again on the next binding pass.
        const value = cachedChars + result.source.length <= maxChars ? result.source : ""
        sources[ticket.address] = value
        cachedChars += value.length
        delete failures[ticket.address]
      }
    }
    revision++
  }

  function retry(ticket) {
    const attempts = (failures[ticket.address] || 0) + 1
    failures[ticket.address] = attempts
    retryAt[ticket.address] = Date.now() + Math.min(30000 * Math.pow(2, attempts - 1), 600000)
  }

  function failed(id) {
    const ticket = takeTicket(id)
    if (!ticket) return
    if (ticket.generation === generation && photosEnabled) retry(ticket)
    revision++
  }

  function disconnected() {
    photosEnabled = false
    generation++
    sources = ({})
    tickets = ({})
    pending = ({})
    retryAt = ({})
    failures = ({})
    cachedChars = 0
    revision++
  }

  Timer {
    interval: 30000
    repeat: true
    running: photos.photosEnabled && photos.revision >= 0 && Object.keys(photos.retryAt).length > 0
    onTriggered: photos.revision++
  }
}
